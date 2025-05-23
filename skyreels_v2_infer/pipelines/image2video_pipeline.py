import os
from typing import List
from typing import Optional
from typing import Union

import numpy as np
import torch
from diffusers.image_processor import PipelineImageInput
from diffusers.video_processor import VideoProcessor
from PIL import Image
from tqdm import tqdm

from ..modules import get_image_encoder
from ..modules import get_text_encoder
from ..modules import get_transformer
from ..modules import get_vae
from ..scheduler.fm_solvers_unipc import FlowUniPCMultistepScheduler


def resizecrop(image: Image.Image, th, tw):
    w, h = image.size
    if w == tw and h == th:
        return image
    if h / w > th / tw:
        new_w = int(w)
        new_h = int(new_w * th / tw)
    else:
        new_h = int(h)
        new_w = int(new_h * tw / th)
    left = (w - new_w) / 2
    top = (h - new_h) / 2
    right = (w + new_w) / 2
    bottom = (h + new_h) / 2
    image = image.crop((left, top, right, bottom))
    return image


class Image2VideoPipeline:
    def __init__(
        self, model_path, dit_path, device: str = "cuda", weight_dtype=torch.bfloat16, use_usp=False, offload=False
    ):
        # 20250423 pftq: Fixed load time by broadcasting transformer and staggering text encoder, VAE, image encoder
        import torch.distributed as dist  
        load_device = "cpu" if offload else device
        self.device = device
        self.offload = offload

        # 20250423 pftq: Check rank and distributed mode
        if use_usp:
            if not dist.is_initialized():
                raise RuntimeError("Distributed environment must be initialized with dist.init_process_group before using use_usp=True")
            local_rank = dist.get_rank()
        else:
            local_rank = 0

        print(f"[Rank {local_rank}] Initializing pipeline components...")

        vae_model_path = os.path.join(model_path, "Wan2.1_VAE.pth")
        # 20250423 pftq: Load normally on single gpu
        if not use_usp:
            print(f"[Rank {local_rank}] Loading transformer to {load_device}...")
            self.transformer = get_transformer(dit_path, load_device, weight_dtype, skip_weights=False)
            print(f"[Rank {local_rank}] Loading text encoder to {load_device}...")
            self.text_encoder = get_text_encoder(model_path, load_device, weight_dtype, skip_weights=False)
            print(f"[Rank {local_rank}] Loading VAE...")
            self.vae = get_vae(vae_model_path, device, weight_dtype=torch.float32)

        # 20250423 pftq: Broadcast transformer from rank 0
        if use_usp:
            broadcast_device = "cpu" # tested to be more stable to start with cpu broadcast even if you have an H100
            if local_rank == 0:
                print(f"[Rank {local_rank}] Loading transformer to {broadcast_device}...")
                self.transformer = get_transformer(dit_path, broadcast_device, weight_dtype, skip_weights=False)
                transformer_state_dict = self.transformer.state_dict() 
            else:
                print(f"[Rank {local_rank}] Skipping transformer load...")
                self.transformer = get_transformer(dit_path, broadcast_device, weight_dtype, skip_weights=True)
                transformer_state_dict = None
            dist.barrier()  # Ensure rank 0 loads transformer and text encoder
            transformer_list = [transformer_state_dict]
            print(f"[Rank {local_rank}] Broadcasting weights for transformer...")
            dist.broadcast_object_list(transformer_list, src=0)
            # 20250423 pftq: Load broadcasted weights on all ranks. Skip redundant load_state_dict on rank 0
            if local_rank != 0:
                print(f"[Rank {local_rank}] Loading broadcasted transformer...")
                transformer_state_dict = transformer_list[0]
                self.transformer.load_state_dict(transformer_state_dict)
            dist.barrier() 
            if offload:
                print(f"[Rank {local_rank}] Moving transformer to cpu...")
                self.transformer.cpu()
            else:
                print(f"[Rank {local_rank}] Moving transformer to {device}...")
                self.transformer.to(device)
            dist.barrier() 
            torch.cuda.empty_cache()
            
            # 20250423 pftq: Broadcast text encoder weights from rank 0
            if local_rank == 0:
                print(f"[Rank {local_rank}] Loading text encoder to {broadcast_device}...")
                self.text_encoder = get_text_encoder(model_path, broadcast_device, weight_dtype, skip_weights=False)
                text_encoder_state_dict = self.text_encoder.state_dict() 
            else:
                print(f"[Rank {local_rank}] Skipping text encoder load...")
                self.text_encoder = get_text_encoder(model_path, broadcast_device, weight_dtype, skip_weights=True)
                text_encoder_state_dict = None
            dist.barrier()  # Ensure rank 0 loads transformer and text encoder
            print(f"[Rank {local_rank}] Broadcasting weights for text encoder...")
            text_encoder_list = [text_encoder_state_dict]
            dist.broadcast_object_list(text_encoder_list, src=0)
            # 20250423 pftq: Load broadcasted weights on all ranks. Skip redundant load_state_dict on rank 0
            if local_rank != 0:
                print(f"[Rank {local_rank}] Loading broadcasted text encoder...")
                text_encoder_state_dict = text_encoder_list[0]
                self.text_encoder.load_state_dict(text_encoder_state_dict)
            dist.barrier() 
            if offload:
                print(f"[Rank {local_rank}] Moving text encoder to cpu...")
                self.text_encoder.cpu()
            else:
                print(f"[Rank {local_rank}] Moving text encoder to {device}...")
                self.text_encoder.to(device)
            dist.barrier() 
            torch.cuda.empty_cache()

            # 20250423 pftq: Stagger VAE loading across ranks
            for rank in range(dist.get_world_size()):
                if local_rank == rank:
                    print(f"[Rank {local_rank}] Loading VAE...")
                    self.vae = get_vae(vae_model_path, device, weight_dtype=torch.float32)
                dist.barrier()  

        # 20250423 pftq: Stagger image encoder loading across ranks
        if use_usp:
            for rank in range(dist.get_world_size()):
                if local_rank == rank:
                    print(f"[Rank {local_rank}] Loading image encoder...")
                    self.clip = get_image_encoder(model_path, load_device, weight_dtype)
                dist.barrier()
        else:
            print(f"[Rank {local_rank}] Loading image encoder...")
            self.clip = get_image_encoder(model_path, load_device, weight_dtype)

        self.sp_size = 1
        self.video_processor = VideoProcessor(vae_scale_factor=16)
        if use_usp:
            from xfuser.core.distributed import get_sequence_parallel_world_size
            from ..distributed.xdit_context_parallel import usp_attn_forward, usp_dit_forward
            import types

            for block in self.transformer.blocks:
                block.self_attn.forward = types.MethodType(usp_attn_forward, block.self_attn)
                # 20250423 pftq: Fixed indentation and removed duplicate forward assignment
                self.transformer.forward = types.MethodType(usp_dit_forward, self.transformer)
            self.sp_size = get_sequence_parallel_world_size()

        self.scheduler = FlowUniPCMultistepScheduler()
        self.vae_stride = (4, 8, 8)
        self.patch_size = (1, 2, 2)

    @torch.no_grad()
    def __call__(
        self,
        image: PipelineImageInput,
        prompt: Union[str, List[str]] = None,
        negative_prompt: Union[str, List[str]] = None,
        height: int = 544,
        width: int = 960,
        num_frames: int = 97,
        num_inference_steps: int = 50,
        guidance_scale: float = 5.0,
        shift: float = 5.0,
        generator: Optional[torch.Generator] = None,
    ):
        F = num_frames

        latent_height = height // 8 // 2 * 2
        latent_width = width // 8 // 2 * 2
        latent_length = (F - 1) // 4 + 1

        h = latent_height * 8
        w = latent_width * 8

        img = self.video_processor.preprocess(image, height=h, width=w)

        img = img.to(device=self.device, dtype=self.transformer.dtype)

        padding_video = torch.zeros(img.shape[0], 3, F - 1, h, w, device=self.device)

        img = img.unsqueeze(2)
        img_cond = torch.concat([img, padding_video], dim=2)
        img_cond = self.vae.encode(img_cond)
        mask = torch.ones_like(img_cond)
        mask[:, :, 1:] = 0
        y = torch.cat([mask[:, :4], img_cond], dim=1)
        self.clip.to(self.device)
        clip_context = self.clip.encode_video(img)
        if self.offload:
            self.clip.cpu()
            torch.cuda.empty_cache()

        # preprocess
        self.text_encoder.to(self.device)
        context = self.text_encoder.encode(prompt).to(self.device)
        context_null = self.text_encoder.encode(negative_prompt).to(self.device)
        if self.offload:
            self.text_encoder.cpu()
            torch.cuda.empty_cache()

        latent = torch.randn(
            16, latent_length, latent_height, latent_width, dtype=torch.float32, generator=generator, device=self.device
        )

        self.transformer.to(self.device)
        with torch.cuda.amp.autocast(dtype=self.transformer.dtype), torch.no_grad():
            self.scheduler.set_timesteps(num_inference_steps, device=self.device, shift=shift)
            timesteps = self.scheduler.timesteps

            arg_c = {
                "context": context,
                "clip_fea": clip_context,
                "y": y,
            }

            arg_null = {
                "context": context_null,
                "clip_fea": clip_context,
                "y": y,
            }

            #self.transformer.to(self.device) # 20250425 pftq: loaded twice
            for _, t in enumerate(tqdm(timesteps)):
                latent_model_input = torch.stack([latent]).to(self.device)
                timestep = torch.stack([t]).to(self.device)
                noise_pred_cond = self.transformer(latent_model_input, t=timestep, **arg_c)[0].to(self.device)
                noise_pred_uncond = self.transformer(latent_model_input, t=timestep, **arg_null)[0].to(self.device)
                noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_cond - noise_pred_uncond)

                temp_x0 = self.scheduler.step(
                    noise_pred.unsqueeze(0), t, latent.unsqueeze(0), return_dict=False, generator=generator
                )[0]
                latent = temp_x0.squeeze(0)
            if self.offload:
                self.transformer.cpu()
                torch.cuda.empty_cache()
            videos = self.vae.decode(latent)
            videos = (videos / 2 + 0.5).clamp(0, 1)
            videos = [video for video in videos]
            videos = [video.permute(1, 2, 3, 0) * 255 for video in videos]
            videos = [video.cpu().numpy().astype(np.uint8) for video in videos]
        return videos
