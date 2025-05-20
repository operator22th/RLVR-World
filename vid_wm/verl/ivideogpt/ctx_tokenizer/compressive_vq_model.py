from typing import *

import torch
import torch.nn as nn

from dataclasses import dataclass
from diffusers.models.autoencoders.vae import VectorQuantizer
from diffusers.configuration_utils import register_to_config, ConfigMixin
from diffusers.models.modeling_utils import ModelMixin
from diffusers.utils import BaseOutput
from diffusers.utils.accelerate_utils import apply_forward_hook
import math

from .vae import Encoder, Decoder
from .conditional_vae import ConditionalEncoder, ConditionalDecoder
from ivideogpt.tokenizer.finite_scalar_quantize import FSQ, get_fsq_levels


@dataclass
class CompressiveVQEncoderOutput(BaseOutput):

    latents: torch.FloatTensor
    dynamics_latents: torch.FloatTensor


@dataclass
class CompressiveVQDecoderOutput(BaseOutput):

    sample: torch.FloatTensor
    ref_sample: Optional[torch.FloatTensor] = None
    commit_loss: Optional[torch.FloatTensor] = None
    dyn_commit_loss: Optional[torch.FloatTensor] = None


class CompressiveVQModelFSQ(ModelMixin, ConfigMixin):

    @register_to_config
    def __init__(
        self,
        in_channels: int = 3,
        out_channels: int = 3,
        down_block_types: Tuple[str, ...] = ("DownEncoderBlock2D",),
        up_block_types: Tuple[str, ...] = ("UpDecoderBlock2D",),
        block_out_channels: Tuple[int, ...] = (64,),
        layers_per_block: int = 1,
        act_fn: str = "silu",
        latent_channels: int = 3,
        sample_size: int = 32,
        vq_fsq_levels: int = 12,
        norm_num_groups: int = 32,
        scaling_factor: float = 0.18215,
        norm_type: str = "group",  # group, spatial
        mid_block_add_attention=True,
        lookup_from_codebook=False,
        force_upcast=False,
        dyn_fsq_levels: int = 12,
        context_length: int = 1,
        max_att_resolution = 32,
        resolution=256,
        patch_size=4,
    ):
        super().__init__()
        
        if isinstance(vq_fsq_levels, int):
            vq_fsq_levels = get_fsq_levels(vq_fsq_levels)
            num_vq_embeddings = math.prod(vq_fsq_levels)
        if isinstance(dyn_fsq_levels, int):
            dyn_fsq_levels = get_fsq_levels(dyn_fsq_levels)
            num_dyn_embeddings = math.prod(dyn_fsq_levels)
        
        self.latent_channels = latent_channels
        self.dyna_latent_channels = latent_channels
        self.context_length = context_length
        self.num_vq_embeddings = num_vq_embeddings
        self.num_dyn_embeddings = num_dyn_embeddings
        self.vq_fsq_levels = vq_fsq_levels
        self.dyn_fsq_levels = dyn_fsq_levels
        self.patch_size = patch_size

        # encoders
        self.cond_encoder = ConditionalEncoder(
            in_channels=in_channels,
            out_channels=self.dyna_latent_channels,
            down_block_types=down_block_types,
            block_out_channels=block_out_channels,
            layers_per_block=layers_per_block,
            act_fn=act_fn,
            norm_num_groups=norm_num_groups,
            double_z=False,
            mid_block_add_attention=True,
            max_att_resolution=max_att_resolution,
            init_resolution=resolution,
            context_length=context_length,
        )

        self.encoder = Encoder(
            in_channels=in_channels,
            out_channels=latent_channels,
            down_block_types=down_block_types,
            block_out_channels=block_out_channels,
            layers_per_block=layers_per_block,
            act_fn=act_fn,
            norm_num_groups=norm_num_groups,
            double_z=False,
            mid_block_add_attention=mid_block_add_attention,
        )

        # vector quantization
        vq_embed_dim = len(self.vq_fsq_levels)
        self.quant_conv = nn.Conv2d(latent_channels, vq_embed_dim, 1)
        self.quantize = FSQ(
            levels=vq_fsq_levels,
        )
        self.post_quant_conv = nn.Conv2d(vq_embed_dim, latent_channels, 1)

        dyn_vq_embed_dim = len(self.dyn_fsq_levels)
        self.quant_linear = nn.Linear(self.dyna_latent_channels * self.patch_size * self.patch_size, dyn_vq_embed_dim)
        self.dynamics_quantize = FSQ(
            levels=dyn_fsq_levels,
        )
        self.post_quant_linear = nn.Linear(dyn_vq_embed_dim, self.dyna_latent_channels * self.patch_size * self.patch_size)

        # decoders
        self.cond_decoder = ConditionalDecoder(
            in_channels=self.dyna_latent_channels,
            out_channels=out_channels,
            up_block_types=up_block_types,
            block_out_channels=block_out_channels,
            layers_per_block=layers_per_block,
            act_fn=act_fn,
            norm_num_groups=norm_num_groups,
            norm_type=norm_type,
            mid_block_add_attention=True,
            max_att_resolution=max_att_resolution,
            init_resolution=32,  # TODO: magic number
            context_length=context_length,
        )

        self.decoder = Decoder(
            in_channels=latent_channels,
            out_channels=out_channels,
            up_block_types=up_block_types,
            block_out_channels=block_out_channels,
            layers_per_block=layers_per_block,
            act_fn=act_fn,
            norm_num_groups=norm_num_groups,
            norm_type=norm_type,
            mid_block_add_attention=mid_block_add_attention,
        )

    @apply_forward_hook
    def encode(self, encoder, x: torch.FloatTensor, return_dict: bool = True) -> CompressiveVQEncoderOutput:
        h, d = encoder(x)
        h = self.quant_conv(h)

        if not return_dict:
            return (h, d)

        return CompressiveVQEncoderOutput(latents=h, dynamics_latents=d)

    @apply_forward_hook
    def decode(
        self, h: torch.FloatTensor, d: torch.FloatTensor,
        force_not_quantize: bool = False, return_dict: bool = True, shape=None
    ) -> Union[CompressiveVQDecoderOutput, torch.FloatTensor]:
        # also go through quantization layer
        quant, info = self.quantize(h)
        commit_loss = torch.tensor(0.0).to(h.device)
        self.code_util = torch.unique(info).shape[0] / self.num_vq_embeddings
        self.unique_code = torch.unique(info)

        d = d.transpose(-1, -2).unsqueeze(-1)  # [B, L, D] => [B, D, L, 1]
        quant_d, dyn_info = self.dynamics_quantize(d)
        dyn_commit_loss = torch.tensor(0.0).to(h.device)
        self.world_code_util = torch.unique(info).shape[0] / self.num_vq_embeddings
        self.world_unique_code = torch.unique(info)
        quant_d = quant_d.squeeze(-1).transpose(-1, -2)  # [B, D, L, 1] => [B, L, D]

        quant2 = self.post_quant_conv(quant)
        quant2_d = self.post_quant_linear(quant_d)

        # de-patchify
        h, w, p, c = quant2.shape[-2], quant2.shape[-1], self.patch_size, self.dyna_latent_channels
        quant2_d = torch.reshape(quant2_d, [quant2_d.shape[0], h // p, w // p, p, p, c])
        quant2_d = torch.einsum("nhwpqc->nchpwq", quant2_d)
        quant2_d = torch.reshape(quant2_d, [quant2_d.shape[0], c, h, w])

        ref_dec, cond_features = self.decoder(quant2, return_features=True)
        if self.context_length > 1:
            B = quant2_d.shape[0] // self.segment_len
            cond_features = [
                f.reshape(B, self.context_length, *f.shape[-3:]).unsqueeze(1).repeat(1, self.segment_len, 1, 1, 1, 1).reshape(-1, self.context_length, *f.shape[-3:]) for f in cond_features
            ]  # B*(T-t), t, C, H, W
        else:
            cond_features = [f.unsqueeze(1).repeat(1, self.segment_len, 1, 1, 1).reshape(-1,
                                                                                        *f.shape[-3:]) for f in cond_features]

        dec = self.cond_decoder(quant2_d, cond_features)

        if not return_dict:
            return (
                dec,
                ref_dec,
                commit_loss,
                dyn_commit_loss,
            )

        return CompressiveVQDecoderOutput(sample=dec, ref_sample=ref_dec, commit_loss=commit_loss, dyn_commit_loss=dyn_commit_loss)

    def forward(
        self, sample: torch.FloatTensor, return_dict: bool = True, return_loss: bool = False,
        segment_len: int = None,
        dyn_sample: torch.FloatTensor = None,
    ) -> Union[CompressiveVQDecoderOutput, Tuple[torch.FloatTensor, ...]]:
        self.segment_len = segment_len

        h, cond_features = self.encoder(sample, return_features=True)
        if self.context_length > 1:
            B = dyn_sample.shape[0] // self.segment_len
            cond_features = [
                f.reshape(B, self.context_length, *f.shape[-3:]).unsqueeze(1).repeat(1, self.segment_len, 1, 1, 1, 1).reshape(-1, self.context_length, *f.shape[-3:]) for f in cond_features
            ]  # B*(T-t), t, C, H, W
        else:
            cond_features = [f.unsqueeze(1).repeat(1, self.segment_len, 1, 1, 1).reshape(-1,
                                                                                        *f.shape[-3:]) for f in cond_features]
        h = self.quant_conv(h)

        d = self.cond_encoder(dyn_sample, cond_features)
        p = self.patch_size
        d = d.permute(0, 2, 3, 1).unfold(1, p, p).unfold(2, p, p).permute(0, 1, 2, 4, 5, 3)  # [B, H/P, W/P, P, P, C]
        d = d.reshape(d.shape[0], d.shape[1] * d.shape[2], -1)
        d = self.quant_linear(d)

        dec = self.decode(h, d)

        if not return_dict:
            if return_loss:
                return (
                    dec.sample,
                    dec.ref_sample,
                    dec.commit_loss,
                    dec.dyn_commit_loss,
                )
            return (dec.sample,)
        if return_loss:
            return dec
        return CompressiveVQDecoderOutput(sample=dec.sample)

    @apply_forward_hook
    def tokenize(self, pixel_values: torch.FloatTensor, context_length: int = 1):
        assert context_length == self.context_length  # TODO: fix

        B, T, C, H, W = pixel_values.shape

        context_frames = pixel_values[:, :context_length].reshape(-1, C, H, W)
        future_frames = pixel_values[:, context_length:].reshape(-1, C, H, W)
        future_length = T - context_length

        # encode context frames
        h, cond_features = self.encoder(context_frames, return_features=True)
        if self.context_length > 1:
            B = future_frames.shape[0] // future_length
            cond_features = [
                f.reshape(B, self.context_length, *f.shape[-3:]).unsqueeze(1)
                .repeat(1, future_length, 1, 1, 1, 1).reshape(-1, self.context_length, *f.shape[-3:])
                for f in cond_features
            ]  # B*(T-t), t, C, H, W
        else:
            cond_features = [
                f.unsqueeze(1).repeat(1, future_length, 1, 1, 1).reshape(-1, *f.shape[-3:])
                for f in cond_features
            ]
        h = self.quant_conv(h)

        # encode future frames conditioned on context
        d = self.cond_encoder(future_frames, cond_features)
        p = self.patch_size
        d = d.permute(0, 2, 3, 1).unfold(1, p, p).unfold(2, p, p).permute(
            0, 1, 2, 4, 5, 3)  # patchify: [B, H/P, W/P, P, P, C]
        d = d.reshape(d.shape[0], d.shape[1] * d.shape[2], -1)
        d = self.quant_linear(d)

        # quantize
        quant, info = self.quantize(h)

        d = d.transpose(-1, -2).unsqueeze(-1)  # [B, L, D] => [B, D, L, 1]
        quant_d, info_d = self.dynamics_quantize(d)

        # flatten into tokens
        indices_c = info.reshape(B, context_length, -1)
        indices_d = info_d.reshape(B, future_length, -1)

        return indices_c, indices_d

    @apply_forward_hook
    def detokenize(self, indices_c, indices_d, context_length: int = 1, cache=None, return_cache=False):
        assert context_length == self.context_length  # TODO: fix
        future_length = indices_d.shape[1]
        ctx_res = (32, 40)  # TODO: magic number
        dyn_res = (8, 10)
        B = indices_c.shape[0]

        # extract embeddings
        indices_c = indices_c.reshape(B, -1)
        indices_d = indices_d.reshape(B, -1)

        quant = self.quantize.indices_to_codes(indices_c)
        quant = quant.reshape(B * context_length, ctx_res[0], ctx_res[1], len(self.vq_fsq_levels)).permute(0, 3, 1,
                                                                                               2)  # [B, D, H, W]
        quant2 = self.post_quant_conv(quant)

        quant_d = self.dynamics_quantize.indices_to_codes(indices_d)
        quant_d = quant_d.reshape(-1, dyn_res[0] * dyn_res[1], len(self.dyn_fsq_levels))  # [B, L, D]
        quant2_d = self.post_quant_linear(quant_d)

        h, w, p, c = quant2.shape[-2], quant2.shape[-1], self.patch_size, self.dyna_latent_channels
        quant2_d = torch.reshape(quant2_d, [quant2_d.shape[0], h // p, w // p, p, p, c])
        quant2_d = torch.einsum("nhwpqc->nchpwq", quant2_d)  # de-patchify
        quant2_d = torch.reshape(quant2_d, [quant2_d.shape[0], c, h, w])

        # decode context frames
        if cache is not None:
            context_dec, cond_features = cache["context_dec"], cache["cond_features"]
        else:
            context_dec, cond_features = self.decoder(quant2, return_features=True)
        if context_length > 1:
            B = quant2_d.shape[0] // future_length
            cond_features = [
                f.reshape(B, context_length, *f.shape[-3:]).unsqueeze(1)
                .repeat(1, future_length, 1, 1, 1, 1).reshape(-1, context_length, *f.shape[-3:])
                for f in cond_features
            ]  # B*(T-t), t, C, H, W
        else:
            cond_features = [f.unsqueeze(1).repeat(1, future_length, 1, 1, 1).reshape(-1, *f.shape[-3:]) for f in
                             cond_features]

        # decode future frames conditioned on context
        dec = self.cond_decoder(quant2_d, cond_features)

        context_dec = context_dec.reshape(B, context_length, *context_dec.shape[-3:])
        dec = dec.reshape(B, future_length, *dec.shape[-3:])

        if return_cache:
            return torch.cat([context_dec, dec], dim=1), {"context_dec": context_dec, "cond_features": cond_features}
        else:
            return torch.cat([context_dec, dec], dim=1)
