#!/usr/bin/env python3
"""Apply the pinned Eden display-lifetime adaptation to the Mesa build export."""
from pathlib import Path
import hashlib
import sys

root = Path(__file__).resolve().parents[1]
source = root.parent/'mihawk-mesa-review/src/vulkan/wsi/wsi_common_videoout.c'
target = root.parent/'mihawk-vulkan-review/.deps/work/radv-src/src/vulkan/wsi/wsi_common_videoout.c'
text = source.read_text()

def replace(old, new):
    global text
    assert text.count(old) == 1, f'Pinned WSI anchor changed: {old[:70]}'
    text = text.replace(old, new)

replace('   bool opened;\n', '   unsigned owners;\n   bool closing, stop;\n   bool opened;\n')
# A set of framebuffers per output size: each remembers the memory to give back.
replace('      uint8_t *buffers;\n   } sets[VIDEOOUT_SIZES];',
        '      uint8_t *buffers;\n      int64_t physical;\n      size_t bytes;\n   } sets[VIDEOOUT_SIZES];')
replace('static int\nsceVideoOutSetFlipRate', 'static int\nsceVideoOutClose(int32_t handle)\n{\n   return 0;\n}\n\nstatic int\nsceVideoOutSetFlipRate')
replace('   uint8_t *buffers = NULL;\n#if defined(__PROSPERO__)\n   int64_t physical = -1;',
        '   uint8_t *buffers = NULL;\n   int64_t physical = -1;\n#if defined(__PROSPERO__)')
replace('   out->sets[index] = (struct wsi_videoout_set){.size = size, .buffers = buffers};',
        '   out->sets[index] = (struct wsi_videoout_set){.size = size, .buffers = buffers, .physical = physical,\n'
        '                                                .bytes = bytes};')
replace('      while (out->queue_count == 0)\n         cnd_wait(&out->changed, &out->lock);',
        '      while (out->queue_count == 0 && !out->stop)\n         cnd_wait(&out->changed, &out->lock);\n      if (out->queue_count == 0 && out->stop) {\n         mtx_unlock(&out->lock);\n         return 0;\n      }')
replace('VkResult\nwsi_display_init_wsi',
        (root/'tools/radv-videoout-lifecycle.inc').read_text()+'\nVkResult\nwsi_display_init_wsi')
# 120 Hz: the app asks for it per game session (headless/display_refresh.h). The pinned source
# decides from the package's param.json, which the app cannot read once it has left the sandbox,
# and for the whole run; the package declares the capability (tools/package-headless-native.sh).
declares = 'videoout_title_declares_high_frame_rate(void)\n{\n'
assert text.count(declares) == 1, 'Pinned WSI anchor changed: the 120 Hz declaration'
body_start = text.index(declares) + len(declares)
body_end = text.index('\n}\n', body_start)
assert 'param.json' in text[body_start:body_end], 'Pinned WSI anchor changed: the 120 Hz declaration reads param.json'
text = (text[:body_start] + '   const char *const asked = getenv("EDEN_VIDEOOUT_120HZ");\n'
        '   return asked != NULL && asked[0] == \'1\';' + text[body_end:])
# The pinned source takes the 120 Hz output back unless a vblank then comes about every 8.3 ms.
# On a display with a variable refresh rate that is wrong: before the first frame is shown it
# idles at the bottom of its range (a vblank every 20.87 ms, 48 Hz, in two reports), the output
# went back to 59.94 Hz, and a game could not have 120 Hz at all. Only a display that stayed at
# 59.94 Hz (a vblank about every 16.7 ms) gives the mode back.
replace('         if (period > VIDEOOUT_HIGH_REFRESH_LIMIT_NS) {',
        '         if (period > UINT64_C(15000000) && period < UINT64_C(18500000)) {')
# Say so when the display follows, as the pinned source does when it does not.
replace('''                    (double)period / 1e6);
         }
      } else {''', '''                    (double)period / 1e6);
         } else {
            fprintf(stderr, "wsi/videoout: 119.88 Hz output, a vblank every %.3f ms\\n", (double)period / 1e6);
         }
      } else {''')
replace('   wsi->base.get_support =', '   videoout_ref();\n\n   wsi->base.get_support =')
replace('   if (wsi)\n      vk_free(alloc, wsi);',
        '   if (wsi) {\n      videoout_unref();\n      vk_free(alloc, wsi);\n      wsi_device->wsi[VK_ICD_WSI_PLATFORM_DISPLAY] = NULL;\n   }')
if sys.argv[1:2] == ['--check']:
    # Is the driver that was built (its recorded display source, EDEN_WSI_SHA256) this adaptation?
    recorded = Path(sys.argv[2]).read_text().split()[0] if Path(sys.argv[2]).is_file() else ''
    sys.exit(0 if recorded == hashlib.sha256(text.encode()).hexdigest() else 1)
changed = not target.exists() or target.read_text() != text
if changed:
    target.write_text(text)
    # Upstream caches the whole build by pin. Invalidate only that receipt;
    # Ninja retains all unchanged objects and rebuilds the patched WSI.
    provenance = root.parent/'mihawk-vulkan-review/.deps/native/radv-release/PROVENANCE.txt'
    if provenance.exists():
        provenance.replace(provenance.with_suffix('.base.txt'))
print(f'RADV WSI adaptation changed={changed} sha256={hashlib.sha256(text.encode()).hexdigest()}')

# Preserve the reason behind RADV's successful-but-empty device enumeration.
# These startup-only diagnostics avoid repeated full game runs for each stage.
for relative, replacements in {
    # Restore the pinned upload path: explicit CPU flush did not affect HUD corruption.
    'src/amd/vulkan/radv_cmd_buffer.c': [
        # EglantinePS5: no stream split inside an emulated task draw. Its chunks' records prepass
        # is a dispatch (radv_before_dispatch splits), after which the chunk's mesh draw went out
        # without the graphics state the split reset. The split stays due and falls before the
        # next draw or dispatch outside the task draw (about 512K words of headroom are left).
        ('static void\nradv_split_cmd_stream_if_due(struct radv_cmd_buffer *cmd_buffer)\n{',
         'static _Thread_local unsigned radv_task_emu_depth;\n\nstatic void\nradv_split_cmd_stream_if_due(struct radv_cmd_buffer *cmd_buffer)\n{'),
        ('   if (likely(!device->ws->cs_split_due) || !device->ws->cs_split_due(cs->b))\n      return;',
         '   if (likely(!device->ws->cs_split_due) || !device->ws->cs_split_due(cs->b))\n      return;\n   if (radv_task_emu_depth)\n      return;'),
        ('static void\nradv_task_emu_draw_direct(struct radv_cmd_buffer *cmd_buffer, uint32_t x, uint32_t y, uint32_t z)\n{',
         'static void\nradv_task_emu_draw_direct_body(struct radv_cmd_buffer *cmd_buffer, uint32_t x, uint32_t y, uint32_t z);\n\n'
         'static void\nradv_task_emu_draw_direct(struct radv_cmd_buffer *cmd_buffer, uint32_t x, uint32_t y, uint32_t z)\n{\n'
         '   radv_task_emu_depth++;\n   radv_task_emu_draw_direct_body(cmd_buffer, x, y, z);\n   radv_task_emu_depth--;\n}\n\n'
         'static void\nradv_task_emu_draw_direct_body(struct radv_cmd_buffer *cmd_buffer, uint32_t x, uint32_t y, uint32_t z)\n{'),
        ('static void\nradv_task_emu_draw_indirect(struct radv_cmd_buffer *cmd_buffer, const struct radv_draw_info *info)\n{',
         'static void\nradv_task_emu_draw_indirect_body(struct radv_cmd_buffer *cmd_buffer, const struct radv_draw_info *info);\n\n'
         'static void\nradv_task_emu_draw_indirect(struct radv_cmd_buffer *cmd_buffer, const struct radv_draw_info *info)\n{\n'
         '   radv_task_emu_depth++;\n   radv_task_emu_draw_indirect_body(cmd_buffer, info);\n   radv_task_emu_depth--;\n}\n\n'
         'static void\nradv_task_emu_draw_indirect_body(struct radv_cmd_buffer *cmd_buffer, const struct radv_draw_info *info)\n{'),
        # The same for a draw whose mesh shader runs as compute (tools/patch-radv-series): its chunks'
        # records pass is a dispatch too, and a split there would leave its copy draws without the
        # graphics state. exploitizm's build has no such guard. The split that falls due goes first,
        # where radv_before_taskmesh_draw puts it for an emulated task draw: the only split point of
        # this draw is the radv_before_draw inside the guard, and a run of these draws alone would
        # otherwise never split and pass the submission limit.
        ('static void\nradv_mesh_compute_draw(struct radv_cmd_buffer *cmd_buffer, const struct radv_draw_info *info, uint32_t x, uint32_t y,\n                       uint32_t z)\n{',
         'static void\nradv_mesh_compute_draw_body(struct radv_cmd_buffer *cmd_buffer, const struct radv_draw_info *info, uint32_t x, uint32_t y,\n                            uint32_t z);\n\n'
         'static void\nradv_mesh_compute_draw(struct radv_cmd_buffer *cmd_buffer, const struct radv_draw_info *info, uint32_t x, uint32_t y,\n                       uint32_t z)\n{\n'
         '   radv_split_cmd_stream_if_due(cmd_buffer);\n   radv_task_emu_depth++;\n   radv_mesh_compute_draw_body(cmd_buffer, info, x, y, z);\n   radv_task_emu_depth--;\n}\n\n'
         'static void\nradv_mesh_compute_draw_body(struct radv_cmd_buffer *cmd_buffer, const struct radv_draw_info *info, uint32_t x, uint32_t y,\n                            uint32_t z)\n{'),
    ],
    'src/amd/vulkan/radv_physical_device.c': [
        ('   if (result == VK_ERROR_INCOMPATIBLE_DRIVER || result == VK_ERROR_INITIALIZATION_FAILED)\n      return VK_SUCCESS;',
         '   if (result != VK_SUCCESS) fprintf(stderr, "radv/ps5: physical device creation result=%d\\n", result);\n   if (result == VK_ERROR_INCOMPATIBLE_DRIVER || result == VK_ERROR_INITIALIZATION_FAILED)\n      return VK_SUCCESS;'),
        ('   if (!pdev->addrlib) {', '   if (!pdev->addrlib) {\n      fprintf(stderr, "radv/ps5: addrlib creation failed\\n");'),
    ],
    'src/amd/vulkan/winsys/ps5/radv_ps5_winsys.c': [
        ('   if (!radv_ps5_platform_init())\n      return;', '   if (!radv_ps5_platform_init()) {\n      fprintf(stderr, "radv/ps5: queue platform initialization failed\\n");\n      return;\n   }'),
        ('   if (!radv_ps5_memory_alloc(RADV_PS5_RING_BYTES, RADV_PS5_LARGE_BYTES, true, &queue->ring))\n      return;', '   if (!radv_ps5_memory_alloc(RADV_PS5_RING_BYTES, RADV_PS5_LARGE_BYTES, true, &queue->ring)) {\n      fprintf(stderr, "radv/ps5: queue ring allocation failed\\n");\n      return;\n   }'),
        ('      radv_ps5_memory_free(&queue->ring);', '      fprintf(stderr, "radv/ps5: queue marker allocation failed\\n");\n      radv_ps5_memory_free(&queue->ring);'),
    ],
    'src/amd/vulkan/winsys/ps5/radv_ps5_platform.c': [
        ('''bool
radv_ps5_memory_grant_gpu(void *address, uint64_t bytes)
{
   /* A title's anonymous memory takes GPU access this way, and a shader then
    * writes it through the CPU's address (HARDWARE_FINDINGS.md, 2026-09-27). */
   return sceKernelMprotect(address, bytes,
                            PS5_KERNEL_PROT_CPU_READ | PS5_KERNEL_PROT_CPU_WRITE | PS5_KERNEL_PROT_GPU_READ |
                               PS5_KERNEL_PROT_GPU_WRITE) == 0;
}''', (root/'tools/radv-host-memory.inc').read_text().strip()),
        ('   if (!placed) {', '   if (!placed) {\n      fprintf(stderr, "radv/ps5: mapping rejected result=%d address=%p bytes=%llu window32=%d\\n", result, address, (unsigned long long)bytes, window32);'),
    ],
}.items():
    # What tools/patch-radv-series.py made of the file (T120's mesh-as-compute series), if it
    # touched it; the pinned checkout otherwise.
    original = root.parent/'mihawk-vulkan-review/.deps/work/radv-series'/relative
    if not original.exists():
        original = root.parent/'mihawk-mesa-review'/relative
    destination = root.parent/'mihawk-vulkan-review/.deps/work/radv-src'/relative
    adapted = original.read_text()
    for old, new in replacements:
        assert adapted.count(old) == 1, f'Pinned initialization anchor changed: {relative}'
        adapted = adapted.replace(old, new)
    if destination.read_text() != adapted:
        destination.write_text(adapted)
        provenance = root.parent/'mihawk-vulkan-review/.deps/native/radv-release/PROVENANCE.txt'
        if provenance.exists():
            provenance.replace(provenance.with_suffix('.base.txt'))
    print(f'RADV initialization diagnostic {relative} sha256={hashlib.sha256(adapted.encode()).hexdigest()}')
