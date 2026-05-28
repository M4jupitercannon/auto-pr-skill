#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="${WORKSPACE:-/workspace/projects}"
PADDLE_DIR="${WORKSPACE}/Paddle"
BUILD_DIR="${PADDLE_DIR}/build"
VENV_DIR="${PADDLE_DIR}/.venv"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
ROCM_SOURCE_PATH="${ROCM_SOURCE_PATH:-$ROCM_PATH}"
AMDGPU_TARGETS="${PADDLE_AMDGPU_TARGETS:-gfx942}"
NINJA_JOBS="${NINJA_JOBS:-16}"
CTEST_JOBS="${CTEST_JOBS:-16}"
CTEST_TIMEOUT="${CTEST_TIMEOUT:-120}"
MIN_TMP_GB=8
GRAPHVIZ_DIR="${WORKSPACE}/graphviz_pkg"
FORCE_SYNC="${FORCE_SYNC:-0}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { echo "[$(date -Is)] $*"; }

rocm_sdk_pythonpath() {
    if [[ -d "/opt/python/lib/python3.13/site-packages" ]]; then
        printf '%s\n' "/opt/python/lib/python3.13/site-packages"
    fi
}

rocm_tool_is_expected() {
    local source_rocm="$1"
    local tool="$2"

    [[ -f "${source_rocm}/bin/${tool}" ]] || [[ -f "/opt/python/bin/${tool}" ]]
}

require_venv() {
    if [[ ! -f "${VENV_DIR}/bin/activate" ]]; then
        log "ERROR: Python venv is missing. Run '$0 venv' first."
        exit 1
    fi
}

check_tmp_space() {
    local avail_kb
    avail_kb=$(df --output=avail /tmp | tail -1 | tr -d ' ')
    local avail_gb=$((avail_kb / 1024 / 1024))
    log "/tmp available: ${avail_gb}GB (need ${MIN_TMP_GB}GB)"
    if (( avail_gb < MIN_TMP_GB )); then
        log "Insufficient /tmp space. Redirecting all temp to workspace."
        export TMPDIR="${BUILD_DIR}/ctest_workspace/tmp"
        export TEMP="$TMPDIR" TMP="$TMPDIR" TEMPDIR="$TMPDIR"
        export HIP_TEMP_DIR="${BUILD_DIR}/ctest_workspace/hipcc_tmp"
        export HIPCC_TEMP_DIR="$HIP_TEMP_DIR"
        mkdir -p "$TMPDIR" "$HIP_TEMP_DIR"
    fi
}

setup_graphviz() {
    if [[ -x "${GRAPHVIZ_DIR}/bin/dot" ]]; then
        log "Graphviz already installed at ${GRAPHVIZ_DIR}"
        return
    fi

    log "Installing Graphviz from conda-forge..."
    local tmp_conda="${BUILD_DIR}/graphviz.conda"
    local tmp_extract="${BUILD_DIR}/graphviz_extracted"
    mkdir -p "$BUILD_DIR"
    curl -fsSL "https://anaconda.org/conda-forge/graphviz/12.2.1/download/linux-64/graphviz-12.2.1-h5ae0cbf_1.conda" -o "$tmp_conda"

    mkdir -p "$tmp_extract" "$GRAPHVIZ_DIR"
    python -c "
import zipfile, tarfile, zstandard, os, sys
z = zipfile.ZipFile('${tmp_conda}')
z.extractall('${tmp_extract}')
pkg = [f for f in os.listdir('${tmp_extract}') if f.startswith('pkg-')][0]
with open(os.path.join('${tmp_extract}', pkg), 'rb') as f:
    dctx = zstandard.ZstdDecompressor()
    with dctx.stream_reader(f) as reader:
        tf = tarfile.open(fileobj=reader, mode='r|')
        tf.extractall('${GRAPHVIZ_DIR}')
        tf.close()
print('Graphviz extracted OK')
"
    rm -rf "$tmp_conda" "$tmp_extract"
    log "Graphviz installed: $(${GRAPHVIZ_DIR}/bin/dot -V 2>&1)"
}

rocm_shim_is_valid() {
    local shim_dir="$1"
    local source_rocm="${2:-$ROCM_SOURCE_PATH}"

    local tool
    for tool in hipcc hipconfig rocminfo rocm-smi rocm_agent_enumerator; do
        if [[ "$tool" == "hipcc" ]] || rocm_tool_is_expected "$source_rocm" "$tool"; then
            [[ -x "${shim_dir}/bin/${tool}" ]] || return 1
        fi
    done
    [[ -e "${shim_dir}/lib" ]] || return 1
    [[ -d "${shim_dir}/cuda/extras/CUPTI" ]] || return 1
}

setup_rocm_shim() {
    local shim_dir="${BUILD_DIR}/rocm-shim"
    local source_rocm="${ROCM_SOURCE_PATH}"

    if [[ -d "$shim_dir" ]]; then
        if rocm_shim_is_valid "$shim_dir" "$source_rocm"; then
            log "ROCm shim already valid at ${shim_dir}"
            export ROCM_PATH="$shim_dir"
            export HIP_PATH="$shim_dir"
            return
        fi

        log "Existing ROCm shim is incomplete; rebuilding ${shim_dir}"
        rm -rf "$shim_dir"
    fi

    if [[ ! -d "$source_rocm" ]]; then
        log "ERROR: ROCm source path does not exist: ${source_rocm}"
        exit 1
    fi

    log "Creating ROCm shim at ${shim_dir} from ${source_rocm}"
    mkdir -p "${shim_dir}/bin" "${shim_dir}/cuda/extras/CUPTI"

    for p in amdgcn clients etc include lib libexec llvm share tests; do
        [[ -e "${source_rocm}/$p" ]] && ln -sf "${source_rocm}/$p" "${shim_dir}/$p"
    done

    # Create wrapper scripts for ROCm tools that have broken shebangs
    local sdk_pypath=""
    sdk_pypath="$(rocm_sdk_pythonpath)"

    for tool in hipcc hipconfig rocminfo rocm-smi rocm_agent_enumerator; do
        if [[ -f "/opt/python/bin/$tool" ]]; then
            if [[ -n "$sdk_pypath" ]]; then
                cat > "${shim_dir}/bin/$tool" <<WRAPPER
#!/usr/bin/env bash
PYTHONPATH=${sdk_pypath} exec /usr/bin/python3 /opt/python/bin/${tool} "\$@"
WRAPPER
            else
                cat > "${shim_dir}/bin/$tool" <<WRAPPER
#!/usr/bin/env bash
exec /usr/bin/python3 /opt/python/bin/${tool} "\$@"
WRAPPER
            fi
            chmod +x "${shim_dir}/bin/$tool"
        elif [[ -f "${source_rocm}/bin/$tool" ]]; then
            ln -sf "${source_rocm}/bin/$tool" "${shim_dir}/bin/$tool"
        fi
    done

    # Link remaining binaries from the real ROCM bin
    for f in "${source_rocm}/bin/"*; do
        [[ -e "$f" ]] || continue
        local name="${f##*/}"
        [[ ! -e "${shim_dir}/bin/$name" ]] && ln -sf "$f" "${shim_dir}/bin/$name"
    done

    if ! rocm_shim_is_valid "$shim_dir" "$source_rocm"; then
        log "ERROR: ROCm shim creation failed; required paths are missing:"
        for tool in hipcc hipconfig rocminfo rocm-smi rocm_agent_enumerator; do
            if [[ "$tool" == "hipcc" ]] || rocm_tool_is_expected "$source_rocm" "$tool"; then
                [[ -x "${shim_dir}/bin/${tool}" ]] || log "ERROR: missing executable ${shim_dir}/bin/${tool}"
            fi
        done
        [[ -e "${shim_dir}/lib" ]] || log "ERROR: missing ${shim_dir}/lib"
        [[ -d "${shim_dir}/cuda/extras/CUPTI" ]] || log "ERROR: missing directory ${shim_dir}/cuda/extras/CUPTI"
        exit 1
    fi

    export ROCM_PATH="$shim_dir"
    export HIP_PATH="$shim_dir"
}

patch_ctest_rocm_ld_library_path() {
    local ctestfile="$1"
    local rocm_lib="${ROCM_PATH}/lib"

    [[ -f "$ctestfile" ]] || return 0

    local result
    result=$(python - "$ctestfile" "$rocm_lib" <<'PY'
import pathlib
import re
import sys

ctest_path = pathlib.Path(sys.argv[1])
current_rocm_lib = sys.argv[2].rstrip("/")
assignment_re = re.compile(r'(?<![A-Za-z0-9_])(LD_LIBRARY_PATH=)([^";\s)]*)')


def is_ld_library_path_var(entry):
    return entry in {"$LD_LIBRARY_PATH", "\\$LD_LIBRARY_PATH"}


def is_stale_rocm_lib(entry):
    normalized = entry.rstrip("/")
    if not normalized or normalized == current_rocm_lib or is_ld_library_path_var(normalized):
        return False

    lower = normalized.lower()
    if not lower.endswith("/lib"):
        return False

    return (
        lower.endswith("/rocm/lib")
        or "/rocm/" in lower
        or "/rocm-" in lower
        or "/rocm_" in lower
        or "/rocm-shim" in lower
    )


def rewrite_assignment(match):
    rewritten_parts = [current_rocm_lib]
    for part in match.group(2).split(":"):
        if part.rstrip("/") == current_rocm_lib or is_stale_rocm_lib(part):
            continue
        rewritten_parts.append(part)
    return match.group(1) + ":".join(rewritten_parts)


text = ctest_path.read_text()
new_text = assignment_re.sub(rewrite_assignment, text)
if new_text != text:
    ctest_path.write_text(new_text)
    print("updated")
else:
    print("unchanged")
PY
)

    if [[ "$result" == "updated" ]]; then
        log "Patched CTestTestfile.cmake with current ROCm lib path"
    fi
}

# ---------------------------------------------------------------------------
# Step 1: Setup venv
# ---------------------------------------------------------------------------
step_venv() {
    log "=== Step 1: Setup Python venv ==="

    if ! command -v uv &>/dev/null; then
        log "ERROR: uv not found. Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
        exit 1
    fi

    if [[ ! -f "${PADDLE_DIR}/python/requirements.txt" ]]; then
        log "ERROR: Paddle source tree is missing. Run '$0 pull' first."
        exit 1
    fi

    if [[ ! -d "$VENV_DIR" ]]; then
        uv python install "${PYTHON_VERSION}" 2>/dev/null || true
        uv venv --relocatable --seed --python "${PYTHON_VERSION}" "$VENV_DIR"
    fi

    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
    export UV_PYTHON="${VENV_DIR}/bin/python"

    log "Installing build dependencies..."
    uv pip install -r "${PADDLE_DIR}/python/requirements.txt"
    uv pip install 'numpy<2.0' func_timeout pandas pyyaml cython wheel \
        decorator astor typing_extensions cmake ninja patchelf

    log "Installing test dependencies..."
    uv pip install scipy xlsxwriter opencv-python-headless hypothesis parameterized \
        wandb soundfile librosa visualdl graphviz paddle2onnx xdoctest gymnasium \
        coverage autograd zstandard apache-tvm-ffi

    log "Venv ready at ${VENV_DIR}"
}

# ---------------------------------------------------------------------------
# Step 2: Git pull
# ---------------------------------------------------------------------------
step_git_pull() {
    log "=== Step 2: Git pull latest develop ==="

    if [[ ! -d "${PADDLE_DIR}/.git" ]]; then
        log "Cloning PaddlePaddle/Paddle..."
        mkdir -p "$WORKSPACE"
        git clone --depth 1 --branch develop https://github.com/PaddlePaddle/Paddle.git "$PADDLE_DIR"
    else
        log "Updating existing repo..."
        cd "$PADDLE_DIR"
        git fetch origin develop --depth 1
        if [[ "$FORCE_SYNC" == "1" ]]; then
            log "FORCE_SYNC=1: resetting local checkout to origin/develop"
            git reset --hard origin/develop
        else
            if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
                log "ERROR: Working tree has tracked local changes. Commit/stash them or rerun with FORCE_SYNC=1."
                exit 1
            fi
            current_branch=$(git rev-parse --abbrev-ref HEAD)
            if [[ "$current_branch" != "develop" ]]; then
                git checkout develop 2>/dev/null || git checkout -b develop origin/develop
            fi
            if ! git merge --ff-only origin/develop; then
                log "Fast-forward merge failed (unrelated histories in shallow clone). Resetting to origin/develop."
                git reset --hard origin/develop
            fi
        fi
    fi

    cd "$PADDLE_DIR"
    log "Commit: $(git rev-parse HEAD)"
    log "Date: $(git log -1 --format='%ci')"
}

# ---------------------------------------------------------------------------
# Step 3: Build
# ---------------------------------------------------------------------------
step_build() {
    log "=== Step 3: CMake configure + build ==="

    mkdir -p "$BUILD_DIR"
    require_venv
    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
    export UV_PYTHON="${VENV_DIR}/bin/python"

    check_tmp_space
    setup_graphviz
    setup_rocm_shim
    export PATH="${ROCM_PATH}/bin:${PATH}"
    export CC=gcc CXX=g++

    local py_inc py_lib
    py_inc=$(python -c "import sysconfig; print(sysconfig.get_paths()['include'])")
    py_lib=$(python -c "import sysconfig, pathlib; print(pathlib.Path(sysconfig.get_config_var('LIBDIR')) / sysconfig.get_config_var('LDLIBRARY'))")

    cd "$BUILD_DIR"

    # OpenBLAS bundled with Paddle has a broken getarch with newer GCC
    export TARGET=SKYLAKEX

    log "Running CMake configure..."
    cmake .. -GNinja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
        -DWITH_GPU=OFF \
        -DWITH_ROCM=ON \
        -DROCM_PATH:PATH="${ROCM_PATH}" \
        -DHIP_PATH:PATH="${HIP_PATH}" \
        -DPADDLE_AMDGPU_TARGETS="${AMDGPU_TARGETS}" \
        -DWITH_TESTING=ON \
        -DWITH_CPP_TEST=OFF \
        -DWITH_DISTRIBUTE=OFF \
        -DWITH_CINN=OFF \
        -DWITH_MKL=OFF \
        -DWITH_AVX=ON \
        -DWITH_PYTHON=ON \
        -DWITH_TENSORRT=OFF \
        -DWITH_ONNXRUNTIME=OFF \
        -DWITH_OPENVINO=OFF \
        -DWITH_INFERENCE_API_TEST=OFF \
        -DWITH_PROFILER=OFF \
        -DPY_VERSION="${PYTHON_VERSION}" \
        -DPYTHON_EXECUTABLE:FILEPATH="${VENV_DIR}/bin/python" \
        -DPYTHON_INCLUDE_DIR:PATH="$py_inc" \
        -DPYTHON_LIBRARY:FILEPATH="$py_lib" \
        -DPYTHON_LIBRARIES:FILEPATH="$py_lib" \
        -DCMAKE_INSTALL_PREFIX="$BUILD_DIR" \
        -DCMAKE_MODULE_PATH="${ROCM_PATH}/lib/cmake/hip"

    log "Building paddle_python target (j${NINJA_JOBS})..."
    ninja -j"${NINJA_JOBS}" paddle_python

    log "Building op_map_codegen..."
    ninja op_map_codegen
    mkdir -p python/paddle/incubate/autograd
    cp "${PADDLE_DIR}/python/paddle/incubate/autograd/phi_ops_map.py" \
       python/paddle/incubate/autograd/phi_ops_map.py 2>/dev/null || true

    log "Building wheel (paddle_copy)..."
    ninja -j"${NINJA_JOBS}" paddle_copy

    local whl
    whl=$(ls python/dist/*.whl 2>/dev/null | head -1)
    if [[ -z "$whl" ]]; then
        log "ERROR: Wheel not found after build!"
        exit 1
    fi
    log "Wheel built: $whl"

    log "Installing wheel into venv..."
    uv pip install "$whl" --no-deps --force-reinstall

    # Create paddlepaddle-gpu metadata alias for paddle2onnx compatibility
    # (ROCm wheel is named paddlepaddle-dcu but paddle2onnx looks for paddlepaddle-gpu)
    local site_pkgs
    site_pkgs=$(python -c "import site; print(site.getsitepackages()[0])")
    local src_dist
    src_dist=$(compgen -G "${site_pkgs}/paddlepaddle_dcu-*.dist-info" | head -1 || true)
    if [[ -n "$src_dist" ]]; then
        local dst_dist="${src_dist/paddlepaddle_dcu/paddlepaddle_gpu}"
        rm -rf "${site_pkgs}"/paddlepaddle_gpu-*.dist-info
        cp -r "$src_dist" "$dst_dist"
        sed -i 's/^Name: paddlepaddle-dcu/Name: paddlepaddle-gpu/' "$dst_dist/METADATA"
        log "Created paddlepaddle-gpu metadata alias"
    fi

    log "Build complete."
}

# ---------------------------------------------------------------------------
# Step 4: Run CTests
# ---------------------------------------------------------------------------
step_test() {
    log "=== Step 4: Run CTests ==="

    require_venv
    if [[ ! -f "${BUILD_DIR}/CTestTestfile.cmake" ]]; then
        log "ERROR: Build tree is missing CTest metadata. Run '$0 build' first."
        exit 1
    fi

    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
    export UV_PYTHON="${VENV_DIR}/bin/python"

    local ctest_workdir="${BUILD_DIR}/ctest_workspace"
    mkdir -p "${ctest_workdir}"/{tmp,paddle_extensions,home,logs,cache,hipcc_tmp}
    check_tmp_space
    setup_graphviz
    setup_rocm_shim

    export HOME="${ctest_workdir}/home"
    export XDG_CACHE_HOME="${ctest_workdir}/cache"
    export PADDLE_EXTENSIONS_DIR="${ctest_workdir}/paddle_extensions"
    export PATH="${GRAPHVIZ_DIR}/bin:${ROCM_PATH}/bin:${PATH}"
    export LD_LIBRARY_PATH="${GRAPHVIZ_DIR}/lib:${ROCM_PATH}/lib:${BUILD_DIR}/python/paddle/base:${BUILD_DIR}/third_party/install/openblas/lib:${BUILD_DIR}/third_party/install/warpctc/lib:${BUILD_DIR}/third_party/install/warprnnt/lib:${BUILD_DIR}/third_party/install/gflags/lib:${BUILD_DIR}/third_party/install/glog/lib:${BUILD_DIR}/third_party/install/protobuf/lib"
    export PYTHONPATH="${BUILD_DIR}/python"
    export HIP_VISIBLE_DEVICES="${HIP_VISIBLE_DEVICES:-0}"

    # Provide missing test_quant_aware.py stub (removed upstream but still imported)
    if [[ ! -f "${BUILD_DIR}/test/quantization/test_quant_aware.py" ]]; then
        log "Creating test_quant_aware.py stub for test_quant_aware_config..."
        python -c "
import inspect, importlib.util, pathlib, sys
src = '${PADDLE_DIR}/test/ir/inference/test_trt_explicit_quantization_mobilenet.py'
spec = importlib.util.spec_from_file_location('_mn', src)
mod = importlib.util.module_from_spec(spec)
sys.modules['_mn'] = mod
# Extract just MobileNet class source
lines = pathlib.Path(src).read_text().splitlines()
in_class = False; cls_lines = ['import paddle', 'from paddle.nn.initializer import KaimingUniform', '']
for line in lines:
    if line.startswith('class MobileNet'):
        in_class = True
    elif in_class and line and not line[0].isspace() and not line.startswith('#'):
        break
    if in_class:
        cls_lines.append(line)
pathlib.Path('${BUILD_DIR}/test/quantization/test_quant_aware.py').write_text('\n'.join(cls_lines))
print('Created test_quant_aware.py stub')
"
    fi

    # Fix C++ test LD_LIBRARY_PATH in generated CTestTestfile
    local ctestfile="${BUILD_DIR}/test/CTestTestfile.cmake"
    patch_ctest_rocm_ld_library_path "$ctestfile"

    # Set FLAGS_enable_pir_api=0 for tests using deprecated paddle.static.nn APIs
    local quant_ctestfile="${BUILD_DIR}/test/quantization/CTestTestfile.cmake"
    if [[ -f "$quant_ctestfile" ]] && ! grep -q "FLAGS_enable_pir_api" "$quant_ctestfile"; then
        log "Patching quantization CTestTestfile for PIR compatibility..."
        sed -i '/test_quant_aware_config/s/ENVIRONMENT/ENVIRONMENT "FLAGS_enable_pir_api=0"/' "$quant_ctestfile" 2>/dev/null || true
        echo 'set_tests_properties(test_quant_aware_config PROPERTIES ENVIRONMENT "FLAGS_enable_pir_api=0")' >> "$quant_ctestfile"
    fi

    cd "$BUILD_DIR"

    local logfile="${ctest_workdir}/logs/ctest-$(date +%Y%m%d-%H%M%S).log"
    log "Running CTest (j${CTEST_JOBS}, timeout=${CTEST_TIMEOUT}s)..."
    log "Log: ${logfile}"

    set +e
    ctest --output-on-failure --timeout "${CTEST_TIMEOUT}" -j"${CTEST_JOBS}" 2>&1 | tee "$logfile"
    local exit_code=$?
    set -e

    local passed failed timeout
    passed=$(grep -c "Passed" "$logfile" 2>/dev/null || echo 0)
    failed=$(grep -c '\*\*\*Failed' "$logfile" 2>/dev/null || echo 0)
    timeout=$(grep -c '\*\*\*Timeout' "$logfile" 2>/dev/null || echo 0)

    log "=== CTest Results ==="
    log "Passed:  ${passed}"
    log "Failed:  ${failed}"
    log "Timeout: ${timeout}"
    log "Exit:    ${exit_code}"
    log "Log:     ${logfile}"

    return $exit_code
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    log "=== Paddle ROCm MI300X CI Script ==="
    log "Workspace: ${WORKSPACE}"
    log "ROCm path: ${ROCM_PATH}"
    log "GPU targets: ${AMDGPU_TARGETS}"

    step_git_pull
    step_venv
    step_build
    step_test

    log "=== Done ==="
}

# Allow running individual steps: ./ci-rocm-mi300x.sh [venv|pull|build|test]
case "${1:-all}" in
    venv)  step_venv ;;
    pull)  step_git_pull ;;
    build) step_build ;;
    test)  step_test ;;
    all)   main ;;
    *)     echo "Usage: $0 [venv|pull|build|test|all]"; exit 1 ;;
esac
