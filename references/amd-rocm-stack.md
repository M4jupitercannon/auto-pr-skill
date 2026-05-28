# AMD ROCm Software Stack Reference

This reference is for auto-pr agents reviewing or editing Paddle changes that
touch AMD GPU, ROCm, HIP, or shared CUDA/HIP code paths. It is intentionally
compact; use it to ask sharper review questions, not as a substitute for
project documentation.

## Stack Layers

From lowest to highest level:

1. Linux kernel driver: `amdgpu`.
2. ROCm runtime: HSA Runtime / ROCR (`hsa-runtime`, `rocr-runtime`).
3. HIP runtime:
   * `hipamd` on AMD hardware.
   * `hip-nvidia` as the CUDA backend compatibility layer.
4. Libraries commonly used by DL frameworks:
   * BLAS: `rocBLAS`, `hipBLAS`.
   * FFT: `rocFFT`, `hipFFT`.
   * DNN kernels: `MIOpen`.
   * Collectives: `RCCL`.
   * Sparse / solver: `rocSPARSE`, `rocSOLVER`.
5. Compiler and tools:
   * `hipcc`, which wraps `amdclang++` for AMD targets.
   * `rocminfo`, `rocm-smi`, `rocm_agent_enumerator`.
   * `hipify-perl` and `hipify-clang` for CUDA-to-HIP migration.

## HIP and CUDA Parity Checks

When a change touches shared GPU code, review both HIP and CUDA behavior:

* Symbol mapping is usually direct but must be guarded correctly:
  `hipMalloc` / `cudaMalloc`, `hipFree` / `cudaFree`,
  `hipStreamSynchronize` / `cudaStreamSynchronize`,
  `hipGetLastError` / `cudaGetLastError`,
  `HIPRT_CB` / `CUDART_CB`.
* Do not replace `cuda*` symbols with `hip*` symbols in code that still builds
  for NVIDIA. Preserve separate branches under `PADDLE_WITH_HIP` and
  `PADDLE_WITH_CUDA` when both products were previously supported.
* AMD wavefront size is commonly 64, while NVIDIA warp size is 32. Kernels
  that hard-code warp assumptions, shuffle widths, lane masks, reductions, or
  launch geometry can silently regress one vendor.
* Some CUDA features have incomplete or version-dependent HIP parity. Check
  graph APIs, cooperative groups, stream capture, event timing, and newer
  runtime attributes before assuming a CUDA feature exists on ROCm.
* HIP error handling still matters. Dropping `hipGetLastError`,
  `hipStreamSynchronize`, or `PADDLE_ENFORCE_GPU_SUCCESS` can leave sticky
  errors that pollute later tests.

## Common AMD GPU Targets

These target names often appear in build flags, CI scripts, and bug reports:

* `gfx906`: MI50 / MI60 class.
* `gfx908`: MI100.
* `gfx90a`: MI200 series such as MI210 / MI250.
* `gfx940`, `gfx941`, `gfx942`: MI300A / MI300X family.

The bundled Paddle profile targets MI300X through `gfx942` by default.

## Build Flags and Environment

Common compile-time macros and runtime environment variables:

* `PADDLE_WITH_HIP`: Paddle HIP/ROCm build.
* `PADDLE_WITH_CUDA`: Paddle CUDA/NVIDIA build.
* `PADDLE_WITH_XPU`: Paddle XPU build.
* `HIP_VISIBLE_DEVICES`, `ROCR_VISIBLE_DEVICES`: device visibility.
* `HSA_OVERRIDE_GFX_VERSION`: compatibility override, useful only for narrow
  debugging and dangerous as a permanent fix.
* `HIPCC_VERBOSE`, `AMD_LOG_LEVEL`: toolchain/runtime diagnostics.
* `PADDLE_AMDGPU_TARGETS`: target list used by this skill's Paddle CI script.

## Paddle ROCm Code Areas

Useful paths when reviewing Paddle changes:

* `paddle/phi/kernels/gpu/`: shared GPU kernels that often compile for both
  CUDA and HIP.
* `paddle/phi/backends/gpu/rocm/`: ROCm-specific backend code.
* `paddle/fluid/platform/device/gpu/rocm/`: ROCm platform/device integration.
* `cmake/` and `CMakeLists.txt`: ROCm/CUDA build matrix wiring.
* `projects/paddle/ci-rocm-mi300x.sh`: this skill's MI300X build/test
  entrypoint.

## Review Questions

Before approving a ROCm-related diff, ask:

* Does this preserve CUDA/NVIDIA behavior, CPU behavior, and XPU behavior?
* Are `PADDLE_WITH_HIP` and `PADDLE_WITH_CUDA` branches both still correct?
* Does the kernel assume warp size 32 or wavefront size 64?
* Did the change weaken numerical tolerance or skip a failure only on AMD?
* Are HIP/CUDA API return values still checked?
* Did build logic change for one backend while silently changing all backends?

## Where to Find More

* ROCm documentation: https://rocm.docs.amd.com/
* ROCm GitHub organization: https://github.com/ROCm
* HIP repository and docs: https://github.com/ROCm/HIP
* HIP/CUDA terminology reference:
  https://github.com/ROCm/HIP/blob/develop/docs/reference/terms.md
* Paddle ROCm docs and code: search the Paddle repository for
  `docs/install/install_ROCM_en.md`, `PADDLE_WITH_HIP`, and
  `paddle/phi/backends/gpu/rocm/`.
