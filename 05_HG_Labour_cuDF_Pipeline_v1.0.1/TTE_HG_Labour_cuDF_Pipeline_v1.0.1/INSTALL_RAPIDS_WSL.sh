#!/usr/bin/env bash
set -euo pipefail

RAPIDS_ENV="tte-rapids"
MINIFORGE_ROOT="${HOME}/miniforge3"
INSTALLER="/tmp/Miniforge3-Linux-x86_64.sh"

echo "Checking WSL GPU access..."
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader

if [[ ! -x "${MINIFORGE_ROOT}/bin/conda" ]]; then
    echo "Installing Miniforge under ${MINIFORGE_ROOT}..."
    curl -fL --retry 3 --output "${INSTALLER}" \
        "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh"
    bash "${INSTALLER}" -b -p "${MINIFORGE_ROOT}"
    rm -f "${INSTALLER}"
fi

CONDA="${MINIFORGE_ROOT}/bin/conda"
"${CONDA}" config --set channel_priority strict

if "${CONDA}" env list | awk '{print $1}' | grep -Fxq "${RAPIDS_ENV}"; then
    echo "Updating existing ${RAPIDS_ENV} environment..."
    "${CONDA}" install -y -n "${RAPIDS_ENV}" \
        -c rapidsai -c conda-forge \
        python=3.12 cudf=26.06 cuda-version=13.2 \
        'pandas>=2.2,<3.0' 'pyarrow>=18' 'openpyxl>=3.1,<4'
else
    echo "Creating ${RAPIDS_ENV} environment..."
    "${CONDA}" create -y -n "${RAPIDS_ENV}" \
        -c rapidsai -c conda-forge \
        python=3.12 cudf=26.06 cuda-version=13.2 \
        'pandas>=2.2,<3.0' 'pyarrow>=18' 'openpyxl>=3.1,<4'
fi

PYTHON="${MINIFORGE_ROOT}/envs/${RAPIDS_ENV}/bin/python"
"${PYTHON}" - <<'PY'
import sys
import cudf
import pandas
import pyarrow
import openpyxl

print("Python:", sys.version.split()[0])
print("cuDF:", cudf.__version__)
print("pandas:", pandas.__version__)
print("pyarrow:", pyarrow.__version__)
print("openpyxl:", openpyxl.__version__)
print(cudf.Series([1, 2, 3]))
print("TTE RAPIDS WSL INSTALLATION PASSED")
PY

echo "RAPIDS Python: ${PYTHON}"
