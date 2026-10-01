"""Building blocks of Meta's watermarking models (Video Seal, Pixel Seal, Watermark Anything).

Adapted from facebookresearch/videoseal (MIT, Copyright (c) Meta Platforms, Inc. and
affiliates) and facebookresearch/watermark-anything (MIT, Copyright (c) Facebook, Inc. and
its affiliates) — see ``THIRD_PARTY_NOTICES.md``. Only the inference-relevant parts are kept
(no temporal pooling, drop-path or checkpointing, no timm/einops dependency); module and
parameter names match the official ``state_dict`` keys so the official weights load strictly.
"""

from __future__ import annotations

from typing import Callable

import torch
import torch.nn.functional as F
from torch import Tensor, nn

RGB_TO_Y = (0.299, 0.587, 0.114)
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)


# -- common layers ---------------------------------------------------------------------------
class LayerNorm(nn.Module):
    """LayerNorm over channels, for ``channels_first`` [B,C,H,W] or ``channels_last`` inputs."""

    def __init__(self, dim: int, eps: float = 1e-6, data_format: str = "channels_first") -> None:
        super().__init__()
        self.weight = nn.Parameter(torch.ones(dim))
        self.bias = nn.Parameter(torch.zeros(dim))
        self.eps = eps
        self.data_format = data_format

    def forward(self, x: Tensor) -> Tensor:
        if self.data_format == "channels_last":
            return F.layer_norm(x, (x.shape[-1],), self.weight, self.bias, self.eps)
        u = x.mean(1, keepdim=True)
        s = (x - u).pow(2).mean(1, keepdim=True)
        x = (x - u) / torch.sqrt(s + self.eps)
        return self.weight[:, None, None] * x + self.bias[:, None, None]


class GRN(nn.Module):
    """Global Response Normalization (ConvNeXt-V2), channels_last input."""

    def __init__(self, dim: int) -> None:
        super().__init__()
        self.gamma = nn.Parameter(torch.zeros(1, 1, 1, dim))
        self.beta = nn.Parameter(torch.zeros(1, 1, 1, dim))

    def forward(self, x: Tensor) -> Tensor:
        gx = torch.norm(x, p=2, dim=(1, 2), keepdim=True)
        nx = gx / (gx.mean(dim=-1, keepdim=True) + 1e-6)
        return self.gamma * (x * nx) + self.beta + x


class ChanRMSNorm(nn.Module):
    """RMS normalization over channels."""

    def __init__(self, dim: int) -> None:
        super().__init__()
        self.scale = dim ** 0.5
        self.gamma = nn.Parameter(torch.ones(dim, 1, 1))

    def forward(self, x: Tensor) -> Tensor:
        return F.normalize(x, dim=1) * self.scale * self.gamma


def get_normalization(name: str) -> Callable[[int], nn.Module]:
    """Normalization layer factory (``batch``, ``group``, ``layer``, ``rms``)."""
    if name.startswith("batch"):
        return nn.BatchNorm2d
    if name.startswith("group"):
        return lambda c: nn.GroupNorm(num_groups=8, num_channels=c)
    if name.startswith("layer"):
        return LayerNorm
    if name.startswith("rms"):
        return ChanRMSNorm
    raise ValueError(f"normalisation inconnue '{name}'")


def get_activation(name: str) -> Callable[[], nn.Module]:
    """Activation factory (``relu``, ``leakyrelu``, ``gelu``, ``silu``)."""
    table: dict[str, Callable[[], nn.Module]] = {
        "relu": nn.ReLU, "gelu": nn.GELU, "silu": nn.SiLU,
        "leakyrelu": lambda: nn.LeakyReLU(negative_slope=0.2),
    }
    if name not in table:
        raise ValueError(f"activation inconnue '{name}'")
    return table[name]


class Upsample(nn.Module):
    """Bilinear ×``factor`` upsampling + reflection-padded 3×3 conv + norm + activation."""

    def __init__(self, in_channels: int, out_channels: int, factor: int,
                 activation: Callable[[], nn.Module],
                 norm_layer: Callable[[int], nn.Module] | None = None) -> None:
        super().__init__()
        norm_layer = norm_layer or LayerNorm
        self.upsample_block = nn.Sequential(
            nn.Upsample(scale_factor=factor, mode="bilinear", align_corners=False),
            nn.ReflectionPad2d(1),
            nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=1, padding=0, bias=False),
            norm_layer(out_channels),
            activation(),
        )

    def forward(self, x: Tensor) -> Tensor:
        return self.upsample_block(x)


# -- message processor -----------------------------------------------------------------------
class MsgProcessor(nn.Module):
    """``binary+concat``: one learned vector per (bit index, bit value), summed over the bits
    and concatenated to the latent map."""

    def __init__(self, nbits: int, hidden_size: int) -> None:
        super().__init__()
        self.nbits = nbits
        self.hidden_size = hidden_size
        self.msg_embeddings = nn.Embedding(2 * nbits, hidden_size)

    def forward(self, latents: Tensor, msg: Tensor) -> Tensor:
        indices = 2 * torch.arange(msg.shape[-1], device=msg.device) + msg.long()   # b k
        emb = self.msg_embeddings(indices).sum(dim=-2)                              # b d
        emb = emb[:, :, None, None].expand(-1, -1, *latents.shape[-2:])
        return torch.cat([latents, emb.to(latents.dtype)], dim=1)


# -- U-Net embedder (Video Seal, Pixel Seal) -------------------------------------------------
class ResnetBlock(nn.Module):
    """(Conv, norm, act) × 2 + 1×1 residual conv."""

    def __init__(self, c_in: int, c_out: int, act: Callable[[], nn.Module],
                 norm: Callable[[int], nn.Module]) -> None:
        super().__init__()
        self.double_conv = nn.Sequential(
            nn.Conv2d(c_in, c_out, kernel_size=3, padding=1, bias=False), norm(c_out), act(),
            nn.Conv2d(c_out, c_out, kernel_size=3, padding=1, bias=False), norm(c_out), act(),
        )
        self.res_conv = nn.Conv2d(c_in, c_out, kernel_size=1)

    def forward(self, x: Tensor) -> Tensor:
        return self.double_conv(x) + self.res_conv(x)


class DBlock(nn.Module):
    """Strided conv (÷2) + ResnetBlock."""

    def __init__(self, c_in: int, c_out: int, act: Callable[[], nn.Module],
                 norm: Callable[[int], nn.Module]) -> None:
        super().__init__()
        self.down = nn.Conv2d(c_in, c_out, kernel_size=3, stride=2, padding=1)
        self.conv = ResnetBlock(c_out, c_out, act, norm)

    def forward(self, x: Tensor) -> Tensor:
        return self.conv(self.down(x))


class UBlock(nn.Module):
    """Upsample (×2) + ResnetBlock."""

    def __init__(self, c_in: int, c_out: int, act: Callable[[], nn.Module],
                 norm: Callable[[int], nn.Module], norm_up: Callable[[int], nn.Module]) -> None:
        super().__init__()
        self.up = Upsample(c_in, c_out, 2, act, norm_up)
        self.conv = ResnetBlock(c_out, c_out, act, norm)

    def forward(self, x: Tensor) -> Tensor:
        return self.conv(self.up(x))


class BottleNeck(nn.Module):
    """``num_blocks`` ResnetBlocks at the lowest resolution."""

    def __init__(self, num_blocks: int, channels: int, act: Callable[[], nn.Module],
                 norm: Callable[[int], nn.Module]) -> None:
        super().__init__()
        self.model = nn.Sequential(*[ResnetBlock(channels, channels, act, norm)
                                     for _ in range(num_blocks)])

    def forward(self, x: Tensor) -> Tensor:
        return self.model(x)


class UNetMsg(nn.Module):
    """U-Net whose bottleneck receives the message embedding (``tanh`` output)."""

    def __init__(self, msg_processor: MsgProcessor, in_channels: int, out_channels: int,
                 z_channels: int, num_blocks: int, activation: str, normalization: str,
                 z_channels_mults: tuple[int, ...], last_tanh: bool = True,
                 normalization_up: str = "layer") -> None:
        super().__init__()
        self.msg_processor = msg_processor
        self.last_tanh = last_tanh
        self.connect_scale = 2 ** -0.5
        norm = get_normalization(normalization)
        norm_up = get_normalization(normalization_up)
        act = get_activation(activation)
        z = [z_channels * m for m in z_channels_mults]
        self.inc = ResnetBlock(in_channels, z[0], act, norm)
        self.downs = nn.ModuleList(DBlock(z[i], z[i + 1], act, norm) for i in range(len(z) - 1))
        z[-1] = z[-1] + msg_processor.hidden_size
        self.bottleneck = BottleNeck(num_blocks, z[-1], act, norm)
        self.ups = nn.ModuleList(UBlock(2 * z[i + 1], z[i], act, norm, norm_up)
                                 for i in reversed(range(len(z) - 1)))
        self.outc = nn.Conv2d(z[0], out_channels, 1)

    def forward(self, imgs: Tensor, msgs: Tensor) -> Tensor:
        hiddens = [self.inc(imgs)]
        for dblock in self.downs:
            hiddens.append(dblock(hiddens[-1]))
        hiddens.append(self.msg_processor(hiddens.pop(), msgs))
        x = self.bottleneck(hiddens[-1])
        for ublock in self.ups:
            x = ublock(torch.cat((x, hiddens.pop() * self.connect_scale), dim=1))
        x = self.outc(x)
        return torch.tanh(x) if self.last_tanh else x


class UnetEmbedder(nn.Module):
    """Video Seal embedder: image (or Y channel) in [0,1] → watermark in [-1,1]."""

    def __init__(self, nbits: int, hidden_size: int, **unet: object) -> None:
        super().__init__()
        self.msg_processor = MsgProcessor(nbits, hidden_size)
        self.unet = UNetMsg(self.msg_processor, **unet)  # type: ignore[arg-type]

    def forward(self, imgs: Tensor, msgs: Tensor) -> Tensor:
        return self.unet(imgs * 2 - 1, msgs)


# -- ConvNeXt-V2 extractor (Video Seal, Pixel Seal) ------------------------------------------
class ConvNeXtBlock(nn.Module):
    """ConvNeXt-V2 block (depthwise 7×7, LayerNorm, MLP with GRN)."""

    def __init__(self, dim: int) -> None:
        super().__init__()
        self.dwconv = nn.Conv2d(dim, dim, kernel_size=7, padding=3, groups=dim)
        self.norm = LayerNorm(dim, eps=1e-6, data_format="channels_last")
        self.pwconv1 = nn.Linear(dim, 4 * dim)
        self.act = nn.GELU()
        self.grn = GRN(4 * dim)
        self.pwconv2 = nn.Linear(4 * dim, dim)

    def forward(self, x: Tensor) -> Tensor:
        y = self.dwconv(x).permute(0, 2, 3, 1)
        y = self.pwconv2(self.grn(self.act(self.pwconv1(self.norm(y)))))
        return x + y.permute(0, 3, 1, 2)


class ConvNeXtV2(nn.Module):
    """ConvNeXt-V2 feature extractor (no pooling, no head)."""

    def __init__(self, depths: tuple[int, ...] = (3, 3, 9, 3),
                 dims: tuple[int, ...] = (96, 192, 384, 768), in_chans: int = 3,
                 stem_stride: int = 4) -> None:
        super().__init__()
        self.downsample_layers = nn.ModuleList([nn.Sequential(
            nn.Conv2d(in_chans, dims[0], kernel_size=4, stride=stem_stride),
            LayerNorm(dims[0], eps=1e-6),
        )])
        for i in range(3):
            self.downsample_layers.append(nn.Sequential(
                LayerNorm(dims[i], eps=1e-6),
                nn.Conv2d(dims[i], dims[i + 1], kernel_size=2, stride=2),
            ))
        self.stages = nn.ModuleList(nn.Sequential(*[ConvNeXtBlock(dims[i])
                                                    for _ in range(depths[i])])
                                    for i in range(4))

    def forward(self, x: Tensor) -> Tensor:
        for down, stage in zip(self.downsample_layers, self.stages):
            x = stage(down(x))
        return x


class PixelDecoder(nn.Module):
    """Video Seal head: upsampling blocks, spatial mean, linear → ``[B, 1+nbits]``."""

    def __init__(self, embed_dim: int, nbits: int, upscale_stages: tuple[int, ...] = (1,)) -> None:
        super().__init__()
        blocks = []
        for factor in upscale_stages:
            blocks.append(Upsample(embed_dim, embed_dim // factor, factor, nn.GELU))
            embed_dim //= factor
        self.output_upscaling = nn.Sequential(*blocks)
        self.linear = nn.Linear(embed_dim, nbits + 1)

    def forward(self, x: Tensor) -> Tensor:
        return self.linear(self.output_upscaling(x).mean(dim=(-2, -1)))


class ConvnextExtractor(nn.Module):
    """Video Seal detector: image in [0,1] → ``[B, 1+nbits]`` logits (channel 0: detection)."""

    def __init__(self, nbits: int, depths: tuple[int, ...], dims: tuple[int, ...]) -> None:
        super().__init__()
        self.convnext = ConvNeXtV2(depths=depths, dims=dims)
        self.pixel_decoder = PixelDecoder(dims[-1], nbits)

    def forward(self, imgs: Tensor) -> Tensor:
        return self.pixel_decoder(self.convnext(imgs * 2 - 1))


# -- JND attenuation -------------------------------------------------------------------------
class JND(nn.Module):
    """Just-Noticeable-Difference heatmap (luminance + contrast masking), in [0,1] units.

    ``in_channels=1`` works on luminance; ``out_channels=3`` repeats the map on RGB (``blue``:
    R and G halved, as in Watermark Anything).
    """

    def __init__(self, in_channels: int = 1, out_channels: int = 1, blue: bool = False) -> None:
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.blue = blue
        kx = torch.tensor([[-1.0, 0.0, 1.0], [-2.0, 0.0, 2.0], [-1.0, 0.0, 1.0]])
        ky = torch.tensor([[1.0, 2.0, 1.0], [0.0, 0.0, 0.0], [-1.0, -2.0, -1.0]])
        kl = torch.tensor([[1.0, 1, 1, 1, 1], [1, 2, 2, 2, 1], [1, 2, 0, 2, 1], [1, 2, 2, 2, 1],
                           [1, 1, 1, 1, 1]])
        for name, kernel in (("kernel_x", kx), ("kernel_y", ky), ("kernel_lum", kl)):
            self.register_buffer(name, kernel.expand(in_channels, 1, *kernel.shape).clone(),
                                 persistent=False)

    @torch.no_grad()
    def heatmaps(self, imgs: Tensor, clc: float = 0.3) -> Tensor:
        """``imgs`` in [0,1] → attenuation map ``[B, out_channels, H, W]``."""
        x = 255.0 * imgs.float()
        if self.in_channels == 1:
            x = RGB_TO_Y[0] * x[:, 0:1] + RGB_TO_Y[1] * x[:, 1:2] + RGB_TO_Y[2] * x[:, 2:3]
        groups = self.in_channels
        la = F.conv2d(x, self.kernel_lum.to(x), padding=2, groups=groups) / 32
        la = torch.where(la <= 127, 17 * (1 - torch.sqrt(la.clamp_min(0) / 127 + 1e-5)),
                         3 / 128 * (la - 127) + 3)
        gx = F.conv2d(x, self.kernel_x.to(x), padding=1, groups=groups)
        gy = F.conv2d(x, self.kernel_y.to(x), padding=1, groups=groups)
        cm = torch.sqrt(gx ** 2 + gy ** 2)
        cm = 0.117 * 16 * cm ** 2.4 / (cm ** 2 + 26 ** 2)
        hmaps = torch.clamp_min(la + cm - clc * torch.minimum(la, cm), 0)
        if self.out_channels == 3 and self.in_channels == 1:
            hmaps = hmaps.repeat(1, 3, 1, 1)
            if self.blue:
                hmaps = hmaps * hmaps.new_tensor([0.5, 0.5, 1.0])[None, :, None, None]
        elif self.out_channels == 1 and self.in_channels == 3:
            hmaps = hmaps.sum(dim=1, keepdim=True) / 3
        return (hmaps / 255).to(imgs.dtype)


def rgb_to_y(x: Tensor) -> Tensor:
    """Luminance (BT.601) ``[B,1,H,W]`` of an RGB batch."""
    return RGB_TO_Y[0] * x[:, 0:1] + RGB_TO_Y[1] * x[:, 1:2] + RGB_TO_Y[2] * x[:, 2:3]


# -- Watermark Anything: VAE embedder --------------------------------------------------------
def _swish(x: Tensor) -> Tensor:
    return x * torch.sigmoid(x)


def _group_norm(channels: int) -> nn.GroupNorm:
    return nn.GroupNorm(num_groups=32, num_channels=channels, eps=1e-6, affine=True)


class VAEResnetBlock(nn.Module):
    """GroupNorm-swish-conv residual block of the LDM autoencoder."""

    def __init__(self, c_in: int, c_out: int) -> None:
        super().__init__()
        self.norm1 = _group_norm(c_in)
        self.conv1 = nn.Conv2d(c_in, c_out, kernel_size=3, stride=1, padding=1)
        self.norm2 = _group_norm(c_out)
        self.conv2 = nn.Conv2d(c_out, c_out, kernel_size=3, stride=1, padding=1)
        if c_in != c_out:
            self.nin_shortcut = nn.Conv2d(c_in, c_out, kernel_size=1)

    def forward(self, x: Tensor) -> Tensor:
        h = self.conv1(_swish(self.norm1(x)))
        h = self.conv2(_swish(self.norm2(h)))
        if hasattr(self, "nin_shortcut"):
            x = self.nin_shortcut(x)
        return x + h


class AttnBlock(nn.Module):
    """Single-head spatial self-attention of the LDM autoencoder."""

    def __init__(self, channels: int) -> None:
        super().__init__()
        self.norm = _group_norm(channels)
        self.q = nn.Conv2d(channels, channels, 1)
        self.k = nn.Conv2d(channels, channels, 1)
        self.v = nn.Conv2d(channels, channels, 1)
        self.proj_out = nn.Conv2d(channels, channels, 1)

    def forward(self, x: Tensor) -> Tensor:
        h = self.norm(x)
        b, c, hh, ww = h.shape
        q = self.q(h).reshape(b, c, hh * ww).permute(0, 2, 1)
        k = self.k(h).reshape(b, c, hh * ww)
        v = self.v(h).reshape(b, c, hh * ww)
        attn = torch.softmax(torch.bmm(q, k) * (int(c) ** -0.5), dim=2)
        out = torch.bmm(v, attn.permute(0, 2, 1)).reshape(b, c, hh, ww)
        return x + self.proj_out(out)


class _Level(nn.Module):
    """One resolution level (``block`` list, empty ``attn``, optional resampling)."""

    def __init__(self) -> None:
        super().__init__()
        self.block = nn.ModuleList()
        self.attn = nn.ModuleList()


class _Down(nn.Module):
    def __init__(self, channels: int) -> None:
        super().__init__()
        self.conv = nn.Conv2d(channels, channels, kernel_size=3, stride=2, padding=0)

    def forward(self, x: Tensor) -> Tensor:
        return self.conv(F.pad(x, (0, 1, 0, 1)))


class _Up(nn.Module):
    def __init__(self, channels: int) -> None:
        super().__init__()
        self.conv = nn.Conv2d(channels, channels, kernel_size=3, stride=1, padding=1)

    def forward(self, x: Tensor) -> Tensor:
        return self.conv(F.interpolate(x, scale_factor=2.0, mode="nearest"))


class _Mid(nn.Module):
    def __init__(self, channels: int) -> None:
        super().__init__()
        self.block_1 = VAEResnetBlock(channels, channels)
        self.attn_1 = AttnBlock(channels)
        self.block_2 = VAEResnetBlock(channels, channels)

    def forward(self, x: Tensor) -> Tensor:
        return self.block_2(self.attn_1(self.block_1(x)))


class VAEEncoder(nn.Module):
    """LDM-style encoder (no attention at intermediate resolutions)."""

    def __init__(self, ch: int, ch_mult: tuple[int, ...], num_res_blocks: int,
                 in_channels: int, z_channels: int) -> None:
        super().__init__()
        self.conv_in = nn.Conv2d(in_channels, ch, kernel_size=3, stride=1, padding=1)
        in_mult = (1,) + tuple(ch_mult)
        self.down = nn.ModuleList()
        block_in = ch
        for level, mult in enumerate(ch_mult):
            down = _Level()
            block_in, block_out = ch * in_mult[level], ch * mult
            for _ in range(num_res_blocks):
                down.block.append(VAEResnetBlock(block_in, block_out))
                block_in = block_out
            if level != len(ch_mult) - 1:
                down.downsample = _Down(block_in)
            self.down.append(down)
        self.mid = _Mid(block_in)
        self.norm_out = _group_norm(block_in)
        self.conv_out = nn.Conv2d(block_in, z_channels, kernel_size=3, stride=1, padding=1)

    def forward(self, x: Tensor) -> Tensor:
        h = self.conv_in(x)
        for down in self.down:
            for block in down.block:
                h = block(h)
            if hasattr(down, "downsample"):
                h = down.downsample(h)
        h = self.mid(h)
        return self.conv_out(_swish(self.norm_out(h)))


class VAEDecoder(nn.Module):
    """LDM-style decoder with ``tanh`` output."""

    def __init__(self, ch: int, ch_mult: tuple[int, ...], num_res_blocks: int,
                 z_channels: int, out_ch: int = 3) -> None:
        super().__init__()
        n = len(ch_mult)
        block_in = ch * ch_mult[-1]
        self.conv_in = nn.Conv2d(z_channels, block_in, kernel_size=3, stride=1, padding=1)
        self.mid = _Mid(block_in)
        ups: list[nn.Module] = []
        for level in reversed(range(n)):
            up = _Level()
            block_out = ch * ch_mult[level]
            for _ in range(num_res_blocks + 1):
                up.block.append(VAEResnetBlock(block_in, block_out))
                block_in = block_out
            if level != 0:
                up.upsample = _Up(block_in)
            ups.insert(0, up)
        self.up = nn.ModuleList(ups)
        self.norm_out = _group_norm(block_in)
        self.conv_out = nn.Conv2d(block_in, out_ch, kernel_size=3, stride=1, padding=1)

    def forward(self, z: Tensor) -> Tensor:
        h = self.mid(self.conv_in(z))
        for up in reversed(self.up):
            for block in up.block:
                h = block(h)
            if hasattr(up, "upsample"):
                h = up.upsample(h)
        return torch.tanh(self.conv_out(_swish(self.norm_out(h))))


class VAEEmbedder(nn.Module):
    """Watermark Anything embedder: normalised image → message-conditioned VAE → ``tanh``."""

    def __init__(self, nbits: int, ch: int = 32, ch_mult: tuple[int, ...] = (1, 1, 1, 2),
                 num_res_blocks: int = 2, z_channels: int = 4) -> None:
        super().__init__()
        hidden = 2 * nbits
        self.encoder = VAEEncoder(ch, ch_mult, num_res_blocks, 3, z_channels)
        self.msg_processor = MsgProcessor(nbits, hidden)
        self.decoder = VAEDecoder(ch, ch_mult, num_res_blocks, z_channels + hidden)

    def forward(self, imgs: Tensor, msgs: Tensor) -> Tensor:
        return self.decoder(self.msg_processor(self.encoder(imgs), msgs))


# -- Watermark Anything: SAM-style ViT extractor ---------------------------------------------
class MLPBlock(nn.Module):
    def __init__(self, dim: int, mlp_dim: int) -> None:
        super().__init__()
        self.lin1 = nn.Linear(dim, mlp_dim)
        self.lin2 = nn.Linear(mlp_dim, dim)
        self.act = nn.GELU()

    def forward(self, x: Tensor) -> Tensor:
        return self.lin2(self.act(self.lin1(x)))


def _rel_pos(q_size: int, k_size: int, rel_pos: Tensor) -> Tensor:
    max_dist = int(2 * max(q_size, k_size) - 1)
    if rel_pos.shape[0] != max_dist:
        rel_pos = F.interpolate(rel_pos.reshape(1, rel_pos.shape[0], -1).permute(0, 2, 1),
                                size=max_dist, mode="linear")
        rel_pos = rel_pos.reshape(-1, max_dist).permute(1, 0)
    q_coords = torch.arange(q_size, device=rel_pos.device)[:, None] * max(k_size / q_size, 1.0)
    k_coords = torch.arange(k_size, device=rel_pos.device)[None, :] * max(q_size / k_size, 1.0)
    coords = (q_coords - k_coords) + (k_size - 1) * max(q_size / k_size, 1.0)
    return rel_pos[coords.long()]


class Attention(nn.Module):
    """Multi-head attention with decomposed relative positions (MViTv2 / SAM)."""

    def __init__(self, dim: int, num_heads: int, input_size: tuple[int, int]) -> None:
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5
        self.qkv = nn.Linear(dim, dim * 3, bias=True)
        self.proj = nn.Linear(dim, dim)
        self.rel_pos_h = nn.Parameter(torch.zeros(2 * input_size[0] - 1, head_dim))
        self.rel_pos_w = nn.Parameter(torch.zeros(2 * input_size[1] - 1, head_dim))

    def forward(self, x: Tensor) -> Tensor:
        b, h, w, _ = x.shape
        qkv = self.qkv(x).reshape(b, h * w, 3, self.num_heads, -1).permute(2, 0, 3, 1, 4)
        q, k, v = qkv.reshape(3, b * self.num_heads, h * w, -1).unbind(0)
        attn = (q * self.scale) @ k.transpose(-2, -1)
        rh, rw = _rel_pos(h, h, self.rel_pos_h), _rel_pos(w, w, self.rel_pos_w)
        r_q = q.reshape(b * self.num_heads, h, w, -1)
        rel_h = torch.einsum("bhwc,hkc->bhwk", r_q, rh)
        rel_w = torch.einsum("bhwc,wkc->bhwk", r_q, rw)
        attn = (attn.view(-1, h, w, h, w) + rel_h[:, :, :, :, None]
                + rel_w[:, :, :, None, :]).view(-1, h * w, h * w)
        x = (attn.softmax(dim=-1) @ v).view(b, self.num_heads, h, w, -1)
        return self.proj(x.permute(0, 2, 3, 1, 4).reshape(b, h, w, -1))


def _window_partition(x: Tensor, size: int) -> tuple[Tensor, tuple[int, int]]:
    b, h, w, c = x.shape
    pad_h, pad_w = (size - h % size) % size, (size - w % size) % size
    if pad_h or pad_w:
        x = F.pad(x, (0, 0, 0, pad_w, 0, pad_h))
    hp, wp = h + pad_h, w + pad_w
    x = x.view(b, hp // size, size, wp // size, size, c)
    return x.permute(0, 1, 3, 2, 4, 5).reshape(-1, size, size, c), (hp, wp)


def _window_unpartition(windows: Tensor, size: int, pad_hw: tuple[int, int],
                        hw: tuple[int, int]) -> Tensor:
    hp, wp = pad_hw
    b = windows.shape[0] // (hp * wp // size // size)
    x = windows.view(b, hp // size, wp // size, size, size, -1)
    x = x.permute(0, 1, 3, 2, 4, 5).reshape(b, hp, wp, -1)
    return x[:, :hw[0], :hw[1], :].contiguous()


class ViTBlock(nn.Module):
    """Transformer block with windowed (``window_size > 0``) or global attention."""

    def __init__(self, dim: int, num_heads: int, mlp_ratio: float, window_size: int,
                 input_size: tuple[int, int]) -> None:
        super().__init__()
        self.norm1 = nn.LayerNorm(dim)
        self.attn = Attention(dim, num_heads,
                              input_size if window_size == 0 else (window_size, window_size))
        self.norm2 = nn.LayerNorm(dim)
        self.mlp = MLPBlock(dim, int(dim * mlp_ratio))
        self.window_size = window_size

    def forward(self, x: Tensor) -> Tensor:
        shortcut = x
        x = self.norm1(x)
        if self.window_size > 0:
            h, w = x.shape[1], x.shape[2]
            x, pad_hw = _window_partition(x, self.window_size)
            x = _window_unpartition(self.attn(x), self.window_size, pad_hw, (h, w))
        else:
            x = self.attn(x)
        x = shortcut + x
        return x + self.mlp(self.norm2(x))


class PatchEmbed(nn.Module):
    def __init__(self, patch_size: int, in_chans: int, embed_dim: int) -> None:
        super().__init__()
        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)

    def forward(self, x: Tensor) -> Tensor:
        return self.proj(x).permute(0, 2, 3, 1)


class ImageEncoderViT(nn.Module):
    """SAM image encoder (absolute + relative positions, windowed/global attention, neck)."""

    def __init__(self, img_size: int = 256, patch_size: int = 16, embed_dim: int = 768,
                 depth: int = 12, num_heads: int = 12, mlp_ratio: float = 4.0,
                 out_chans: int = 768, window_size: int = 8,
                 global_attn_indexes: tuple[int, ...] = (2, 5, 8, 11)) -> None:
        super().__init__()
        grid = img_size // patch_size
        self.patch_embed = PatchEmbed(patch_size, 3, embed_dim)
        self.pos_embed = nn.Parameter(torch.zeros(1, grid, grid, embed_dim))
        self.blocks = nn.ModuleList(
            ViTBlock(embed_dim, num_heads, mlp_ratio,
                     0 if i in global_attn_indexes else window_size, (grid, grid))
            for i in range(depth))
        self.neck = nn.Sequential(
            nn.Conv2d(embed_dim, out_chans, kernel_size=1, bias=False),
            LayerNorm(out_chans),
            nn.Conv2d(out_chans, out_chans, kernel_size=3, padding=1, bias=False),
            LayerNorm(out_chans),
        )

    def forward(self, x: Tensor) -> Tensor:
        x = self.patch_embed(x) + self.pos_embed
        for block in self.blocks:
            x = block(x)
        return self.neck(x.permute(0, 3, 1, 2))


class WamPixelDecoder(nn.Module):
    """Watermark Anything head: ×16 upsampling → per-pixel ``1 + nbits`` logits."""

    def __init__(self, embed_dim: int, nbits: int,
                 upscale_stages: tuple[int, ...] = (4, 2, 2)) -> None:
        super().__init__()
        blocks = []
        for factor in upscale_stages:
            blocks.append(Upsample(embed_dim, embed_dim // factor, factor, nn.GELU))
            embed_dim //= factor
        self.output_upscaling = nn.Sequential(*blocks)
        self.last_layer = nn.Conv2d(embed_dim, nbits + 1, kernel_size=1, bias=True)

    def forward(self, x: Tensor) -> Tensor:
        return self.last_layer(self.output_upscaling(x))


class SegmentationExtractor(nn.Module):
    """Watermark Anything detector: normalised image → ``[B, 1+nbits, H, W]`` logits."""

    def __init__(self, nbits: int, img_size: int = 256) -> None:
        super().__init__()
        self.image_encoder = ImageEncoderViT(img_size=img_size)
        self.pixel_decoder = WamPixelDecoder(768, nbits)

    def forward(self, imgs: Tensor) -> Tensor:
        return self.pixel_decoder(self.image_encoder(imgs))
