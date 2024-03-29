# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Helper functions for visualizing things."""

import torch
from internal import stepfun
from internal import utils
from matplotlib import cm
from internal import math, image
import os
import numpy as np
import mediapy as media

def weighted_percentile(x, weight, ps, assume_sorted=False):
    """Compute the weighted percentile(s) of a single vector."""
    x = x.reshape([-1])
    weight = weight.reshape([-1])
    if not assume_sorted:
        sortidx = torch.argsort(x)
        x, weight = x[sortidx], weight[torch.remainder(sortidx, len(weight))]

    acc_w = torch.cumsum(weight, dim=0)
    ps = torch.tensor(ps, device=x.device)
    return math.interp(ps * acc_w[-1] / 100, acc_w, x)


def sinebow(h):
    """A cyclic and uniform colormap, see http://basecase.org/env/on-rainbows."""
    def f(x): return torch.sin(torch.pi * x)**2
    return torch.stack([f(3 / 6 - h), f(5 / 6 - h), f(7 / 6 - h)], -1)


def matte(vis, acc, dark=0.8, light=1.0, width=8):
    """Set non-accumulated pixels to a Photoshop-esque checker pattern."""
    bg_mask = torch.logical_xor(
        (torch.arange(acc.shape[0]) % (2 * width) // width)[:, None],
        (torch.arange(acc.shape[1]) % (2 * width) // width)[None, :])
    bg = torch.where(bg_mask, light, dark)
    bg = bg.to(vis.device)
    return vis * acc[:, :, None] + (bg * (1 - acc))[:, :, None]


def visualize_cmap(value,
                   weight,
                   colormap,
                   lo=None,
                   hi=None,
                   percentile=99.,
                   curve_fn=lambda x: x,
                   modulus=None,
                   matte_background=True):
    """Visualize a 1D image and a 1D weighting according to some colormap.

    Args:
      value: A 1D image.
      weight: A weight map, in [0, 1].
      colormap: A colormap function.
      lo: The lower bound to use when rendering, if None then use a percentile.
      hi: The upper bound to use when rendering, if None then use a percentile.
      percentile: What percentile of the value map to crop to when automatically
        generating `lo` and `hi`. Depends on `weight` as well as `value'.
      curve_fn: A curve function that gets applied to `value`, `lo`, and `hi`
        before the rest of visualization. Good choices: x, 1/(x+eps), log(x+eps).
      modulus: If not None, mod the normalized value by `modulus`. Use (0, 1]. If
        `modulus` is not None, `lo`, `hi` and `percentile` will have no effect.
      matte_background: If True, matte the image over a checkerboard.

    Returns:
      A colormap rendering.
    """


    # Identify the values that bound the middle of `value' according to `weight`.
    lo_auto, hi_auto = weighted_percentile(
        value, weight, [50 - percentile / 2, 50 + percentile / 2])

    # If `lo` or `hi` are None, use the automatically-computed bounds above.
    eps = torch.tensor(torch.finfo(torch.float32).eps)
    lo = lo or (lo_auto - eps)
    hi = hi or (hi_auto + eps)

    # Curve all values.
    value, lo, hi = [curve_fn(x) for x in [value, lo, hi]]

    # Wrap the values around if requested.
    if modulus:
        value = torch.mod(value, modulus) / modulus
    else:
        # Otherwise, just scale to [0, 1].
        value = torch.nan_to_num(
            torch.clip((value - torch.min(lo, hi)) / torch.abs(hi - lo), 0, 1))

    if colormap:
        colorized = torch.tensor(colormap(value.cpu())[:, :, :3], dtype=torch.float32, device=value.device)
    else:
        if len(value.shape) != 3:
            raise ValueError(
                f'value must have 3 dims but has {len(value.shape)}')
        if value.shape[-1] != 3:
            raise ValueError(
                f'value must have 3 channels but has {len(value.shape[-1])}')
        colorized = value

    return matte(colorized, weight) if matte_background else colorized


def visualize_coord_mod(coords, acc):
    """Visualize the coordinate of each point within its "cell"."""
    return matte(((coords + 1) % 2) / 2, acc)


def visualize_rays(dist,
                   dist_range,
                   weights,
                   rgbs,
                   accumulate=False,
                   renormalize=False,
                   resolution=2048,
                   bg_color=0.8):
    """Visualize a bundle of rays."""
    dist_vis = torch.linspace(*dist_range, resolution + 1, device=dist[0].device)
    vis_rgb, vis_alpha = [], []
    for ds, ws, rs in zip(dist, weights, rgbs):
        vis_rs, vis_ws = [], []
        for d, w, r in zip(ds, ws, rs):
            if accumulate:
                # Produce the accumulated color and weight at each point along the ray.
                w_csum = torch.cumsum(w, dim=0)
                rw_csum = torch.cumsum((r * w[:, None]), dim=0)
                eps = torch.finfo(torch.float32).eps
                r, w = (rw_csum + eps) / (w_csum[:, None] + 2 * eps), w_csum
            vis_rs.append(stepfun.resample(dist_vis, d, r.T, use_avg=True).T)
            vis_ws.append(stepfun.resample(dist_vis, d, w.T, use_avg=True).T)
        vis_rgb.append(torch.stack(vis_rs))
        vis_alpha.append(torch.stack(vis_ws))
    vis_rgb = torch.stack(vis_rgb, dim=1)
    vis_alpha = torch.stack(vis_alpha, dim=1)

    if renormalize:
        # Scale the alphas so that the largest value is 1, for visualization.
        vis_alpha /= torch.max(torch.finfo(torch.float32).eps,
                               torch.max(vis_alpha))

    if resolution > vis_rgb.shape[0]:
        rep = resolution // (vis_rgb.shape[0] * vis_rgb.shape[1] + 1)
        stride = rep * vis_rgb.shape[1]

        vis_rgb = torch.tile(vis_rgb, (1, 1, rep, 1)).reshape(
            (-1,) + vis_rgb.shape[2:])
        vis_alpha = torch.tile(vis_alpha, (1, 1, rep)).reshape(
            (-1,) + vis_alpha.shape[2:])

        # Add a strip of background pixels after each set of levels of rays.
        vis_rgb = vis_rgb.reshape((-1, stride) + vis_rgb.shape[1:])
        vis_alpha = vis_alpha.reshape((-1, stride) + vis_alpha.shape[1:])
        vis_rgb = torch.cat([vis_rgb, torch.zeros_like(vis_rgb[:, :1])],
                            dim=1).reshape((-1,) + vis_rgb.shape[2:])
        vis_alpha = torch.cat(
            [vis_alpha, torch.zeros_like(vis_alpha[:, :1])],
            dim=1).reshape((-1,) + vis_alpha.shape[2:])

    # Matte the RGB image over the background.
    vis = vis_rgb * vis_alpha[..., None] + \
        (bg_color * (1 - vis_alpha))[..., None]

    # Remove the final row of background pixels.
    vis = vis[:-1]
    vis_alpha = vis_alpha[:-1]
    return vis, vis_alpha


def visualize_suite(rendering, rays, linear_to_srgb):
    """A wrapper around other visualizations for easy integration."""

    def depth_curve_fn(x):
        log_ = -torch.log(x + torch.finfo(torch.float32).eps)
        return log_.to(x.device)
    rgb = image.linear_to_srgb(rendering['rgb']) if linear_to_srgb \
        else rendering['rgb']
    acc = rendering['acc']

    distance_mean = rendering['distance_mean']
    distance_median = rendering['distance_median']
    distance_p5 = rendering['distance_percentile_5']
    distance_p95 = rendering['distance_percentile_95']
    acc = torch.where(torch.isnan(distance_mean), torch.zeros_like(acc), acc)
    acc.to(distance_mean.device)

    # The xyz coordinates where rays terminate.
    coords = rays.origins + rays.directions * distance_mean[:, :, None]
    
    vis_depth_mean, vis_depth_median = [
        visualize_cmap(x, acc, cm.get_cmap('turbo'), curve_fn=depth_curve_fn)
        for x in [distance_mean, distance_median]
    ]

    # Render three depth percentiles directly to RGB channels, where the spacing
    # determines the color. delta == big change, epsilon = small change.
    #   Gray: A strong discontinuitiy, [x-epsilon, x, x+epsilon]
    #   Purple: A thin but even density, [x-delta, x, x+delta]
    #   Red: A thin density, then a thick density, [x-delta, x, x+epsilon]
    #   Blue: A thick density, then a thin density, [x-epsilon, x, x+delta]
    depth_triplet = torch.stack([2 * distance_median - distance_p5,
                                 distance_median, distance_p95], dim=-1)
    vis_depth_triplet = visualize_cmap(
        depth_triplet, acc, None,
        curve_fn=lambda x: torch.log(x + torch.finfo(torch.float32).eps))

    dist = rendering['ray_sdist']
    dist_range = (0, 1)
    weights = rendering['ray_weights']
    rgbs = [torch.clip(r, 0, 1) for r in rendering['ray_rgbs']]

    vis_ray_colors, _ = visualize_rays(dist, dist_range, weights, rgbs)

    sqrt_weights = [torch.sqrt(w) for w in weights]
    sqrt_ray_weights, ray_alpha = visualize_rays(
        dist,
        dist_range,
        [torch.ones_like(lw) for lw in sqrt_weights],
        [lw[..., None] for lw in sqrt_weights],
        bg_color=0,
    )
    sqrt_ray_weights = sqrt_ray_weights[..., 0]
    # print(len(sqrt_weights), sqrt_weights[0].shape, len(sqrt_ray_weights), sqrt_ray_weights[0].shape)

    null_color = torch.tensor([1., 0., 0.], device=ray_alpha.device)
    vis_ray_weights_cmap = visualize_cmap(
            sqrt_ray_weights,
            torch.ones_like(sqrt_ray_weights),
            cm.get_cmap('gray'),
            lo=torch.tensor(0),
            hi=torch.tensor(1),
            matte_background=False,
    )

    vis_ray_weights = torch.where(
        ray_alpha[:, :, None] == 0,
        null_color[None, None],
        vis_ray_weights_cmap
    )

    vis = {
        'color': rgb,
        'acc': acc,
        'color_matte': matte(rgb, acc),
        'depth_mean': vis_depth_mean,
        'depth_median': vis_depth_median,
        'depth_triplet': vis_depth_triplet,
        'coords_mod': visualize_coord_mod(coords, acc),
        'ray_colors': vis_ray_colors,
        'ray_weights': vis_ray_weights,
    }

    if 'rgb_cc' in rendering:
        vis['color_corrected'] = rendering['rgb_cc']

    # Render every item named "normals*".
    for key, val in rendering.items():
        if key.startswith('normals'):
            vis[key] = matte(val / 2. + 0.5, acc)

    if 'roughness' in rendering:
        vis['roughness'] = matte(torch.tanh(rendering['roughness']), acc)
    if 'diffuse' in rendering:
        diffuse_rgb = image.linear_to_srgb(rendering['diffuse']) if linear_to_srgb \
            else rendering['diffuse']
        vis['diffuse'] = diffuse_rgb
        vis['diffuse_matte'] = matte(diffuse_rgb, acc)
    if 'specular' in rendering:
        specular_rgb = image.linear_to_srgb(rendering['specular']) if linear_to_srgb \
            else rendering['specular']
        vis['specular'] = specular_rgb
        vis['specular_matte'] = matte(specular_rgb, acc)
    if 'tint' in rendering:
        vis['tint'] = rendering['tint']
        vis['tint_matte'] = matte(rendering['tint'], acc)


    return vis


def create_videos(config, base_dir, out_dir, out_name, num_frames):
    """Creates videos out of the images saved to disk."""
    names = [n for n in config.checkpoint_dir.split('/') if n]
    # Last two parts of checkpoint path are experiment name and scene name.
    exp_name, scene_name = names[-2:]
    video_prefix = f'{scene_name}_{exp_name}_{out_name}'

    zpad = max(3, len(str(num_frames - 1)))

    def idx_to_str(idx):
        return str(idx).zfill(zpad)

    utils.makedirs(base_dir)

    # Load one example frame to get image shape and depth range.
    depth_file = os.path.join(out_dir, f'distance_mean_{idx_to_str(0)}.tiff')
    depth_frame = utils.load_img(depth_file)
    shape = depth_frame.shape
    p = config.render_dist_percentile
    distance_limits = np.percentile(depth_frame.flatten(), [p, 100 - p])
    lo, hi = [config.render_dist_curve_fn(x) for x in distance_limits]
    print(f'Video shape is {shape[:2]}')

    video_kwargs = {
        'shape': shape[:2],
        'codec': 'h264',
        'fps': config.render_video_fps,
        'crf': config.render_video_crf,
    }

    for k in ['color', 'diffuse', 'specular', 'normals_pred', 'acc', 'distance_mean', 'distance_median']:
        video_file = os.path.join(base_dir, f'{video_prefix}_{k}.mp4')
        input_format = 'gray' if k == 'acc' else 'rgb'
        file_ext = 'png' if k in ['color', 'diffuse', 'specular', 'normals_pred'] else 'tiff'
        idx = 0
        file0 = os.path.join(out_dir, f'{k}_{idx_to_str(0)}.{file_ext}')
        if not utils.file_exists(file0):
            print(f'Images missing for tag {k}')
            continue
        print(f'Making video {video_file}...')
        with media.VideoWriter(
                video_file, **video_kwargs, input_format=input_format) as writer:
            for idx in range(num_frames):
                img_file = os.path.join(
                    out_dir, f'{k}_{idx_to_str(idx)}.{file_ext}')
                if not utils.file_exists(img_file):
                    raise ValueError(f'Image file {img_file} does not exist.')
                img = utils.load_img(img_file)
                if k in ['color', 'diffuse', 'specular', 'normals_pred']:
                    img = img / 255.
                elif k.startswith('distance'):
                    img = config.render_dist_curve_fn(img)
                    img = np.clip((img - np.minimum(lo, hi)) /
                                  np.abs(hi - lo), 0, 1)
                    img = cm.get_cmap('turbo')(img)[..., :3]

                frame = (np.clip(np.nan_to_num(img), 0., 1.)
                         * 255.).astype(np.uint8)
                writer.add_image(frame)
                idx += 1