import logging
import torch
from torch import nn
from torch.nn import functional as F
from modules import CrossmodalTransformer
from fairseq.modules import PositionalEmbedding

logger = logging.getLogger(__name__)


class MULTModel(nn.Module):
    def __init__(
        self,
        only_vision,
        only_audio,
        only_text,
        orig_d_v,
        orig_d_a,
        orig_d_t,
        n_head,
        n_cmlayer,
        d_out=8,
        d_model=40,
        emb_dropout=0.25,
        attn_dropout=0.1,
        attn_dropout_audio=0.0,
        attn_dropout_vision=0.0,
        relu_dropout=0.1,
        res_dropout=0.1,
        out_dropout=0.0,
        max_position=128,
        attn_mask=True,
        scale_embedding=True,
    ):
        super(MULTModel, self).__init__()

        # Wheter to use whole mode or not
        do = only_vision + only_audio + only_text
        assert do == 0 or do == 1
        self.partial_mode = True if do == 1 else False

        self.do_vision, self.do_audio, self.do_text = (
            (1, 1, 1) if not self.partial_mode else (only_vision, only_audio, only_text)
        )
        self.d_model = d_model
        self.emb_dropout = emb_dropout
        self.out_dropout = out_dropout

        combined_dim = 2 * d_model if self.partial_mode == 1 else 6 * d_model

        # Input Encoder (Temporal convolution layers) -> (B, orig_d, L) => (B, d, L)
        self.vision_encoder = nn.Conv1d(orig_d_v, d_model, kernel_size=3, padding=0, bias=False)
        self.audio_encoder = nn.Conv1d(orig_d_a, d_model, kernel_size=5, padding=0, bias=False)
        self.text_encoder = nn.Conv1d(orig_d_t, d_model, kernel_size=3, padding=0, bias=False)

        # Cross-modal Transformer layers -> (B, L, d) => (L, B, d)
        if self.do_vision:
            self.vision_layers_with_audio = CrossmodalTransformer(
                d_model,
                n_head,
                emb_dropout,
                attn_dropout_vision,
                res_dropout,
                relu_dropout,
                n_cmlayer,
                attn_mask,
            )
            self.vision_layers_with_text = CrossmodalTransformer(
                d_model,
                n_head,
                emb_dropout,
                attn_dropout_vision,
                res_dropout,
                relu_dropout,
                n_cmlayer,
                attn_mask,
            )

        if self.do_audio:
            self.audio_layers_with_vision = CrossmodalTransformer(
                d_model,
                n_head,
                emb_dropout,
                attn_dropout_audio,
                res_dropout,
                relu_dropout,
                n_cmlayer,
                attn_mask,
            )
            self.audio_layers_with_text = CrossmodalTransformer(
                d_model,
                n_head,
                emb_dropout,
                attn_dropout_audio,
                res_dropout,
                relu_dropout,
                n_cmlayer,
                attn_mask,
            )

        if self.do_text:
            self.text_layers_with_vision = CrossmodalTransformer(
                d_model,
                n_head,
                emb_dropout,
                attn_dropout,
                res_dropout,
                relu_dropout,
                n_cmlayer,
                attn_mask,
            )
            self.text_layers_with_audio = CrossmodalTransformer(
                d_model,
                n_head,
                emb_dropout,
                attn_dropout,
                res_dropout,
                relu_dropout,
                n_cmlayer,
                attn_mask,
            )

        # Self-Attention layers -> (B, L, d) => (L, B, d)
        self.vision_layers = CrossmodalTransformer(
            2 * d_model,
            n_head,
            emb_dropout,
            attn_dropout,
            res_dropout,
            relu_dropout,
            n_cmlayer,
            attn_mask,
        )
        self.audio_layers = CrossmodalTransformer(
            2 * d_model,
            n_head,
            emb_dropout,
            attn_dropout,
            res_dropout,
            relu_dropout,
            n_cmlayer,
            attn_mask,
        )
        self.text_layers = CrossmodalTransformer(
            2 * d_model,
            n_head,
            emb_dropout,
            attn_dropout,
            res_dropout,
            relu_dropout,
            n_cmlayer,
            attn_mask,
        )

        # Projection layers
        self.fc_layer1 = nn.Linear(combined_dim, combined_dim)
        self.fc_layer2 = nn.Linear(combined_dim, combined_dim)
        self.out_layer = nn.Linear(combined_dim, d_out)

    def forward(self, x_vision, x_audio, x_text):
        """
            Args:
        x_vision, x_audio, x_text : input tensor -> (B, L, d)
        """
        # (B, L, d) => (B, d, L)
        x_vision = x_vision.transpose(1, 2)
        x_audio = x_audio.transpose(1, 2)
        x_text = F.dropout(x_text.transpose(1, 2), self.emb_dropout, self.training)

        # (B, d, L) => (B, L, d)
        x_vision = self.vision_encoder(x_vision).transpose(1, 2)
        x_audio = self.audio_encoder(x_audio).transpose(1, 2)
        x_text = self.text_encoder(x_text).transpose(1, 2)

        # Crossmodal Attention
        last_hidden = []
        if self.do_vision:
            x_vision_with_audio = self.vision_layers_with_audio(x_vision, x_audio)
            x_vision_with_text = self.vision_layers_with_text(x_vision, x_text)
            x_vision2 = torch.cat([x_vision_with_audio, x_vision_with_text], dim=2).transpose(0, 1)
            x_vision2 = self.vision_layers(x_vision2)[-1]
            last_hidden.append(x_vision2)  # take it from last time step

        if self.do_audio:
            x_audio_with_vision = self.audio_layers_with_vision(x_audio, x_vision)
            x_audio_with_text = self.audio_layers_with_text(x_audio, x_text)
            x_audio2 = torch.cat([x_audio_with_vision, x_audio_with_text], dim=2).transpose(0, 1)
            x_audio2 = self.audio_layers(x_audio2)[-1]
            last_hidden.append(x_audio2)

        if self.do_text:
            x_text_with_vision = self.text_layers_with_vision(x_text, x_vision)
            x_text_with_audio = self.text_layers_with_audio(x_text, x_audio)
            x_text2 = torch.cat([x_text_with_vision, x_text_with_audio], dim=2).transpose(0, 1)
            x_text2 = self.text_layers(x_text2)[-1]
            last_hidden.append(x_text2)

        last_hidden = last_hidden[0] if self.partial_mode else torch.cat(last_hidden, dim=1)

        out = F.relu(self.fc_layer1(last_hidden))
        out = self.fc_layer2(F.dropout(out, p=self.out_dropout, training=self.training))
        out = out + last_hidden

        out = self.out_layer(out)
        return out, last_hidden
