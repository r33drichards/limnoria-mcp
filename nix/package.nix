{ python3Packages }:
python3Packages.buildPythonPackage {
  pname = "limnoria-mcp";
  version = "0.1.0";
  pyproject = true;
  src = ../.;
  # -I intentionally ignores PYTHONPATH. Bake only this immutable dependency
  # path into the trusted worker; never take an import path from a request/env.
  postPatch = ''
    substituteInPlace src/limnoria_mcp/tcl_worker.py \
      --replace-fail "TKINTER_SEARCH_PATH = None" \
      "TKINTER_SEARCH_PATH = '${python3Packages.tkinter}/${python3Packages.python.sitePackages}'"
  '';
  build-system = [ python3Packages.setuptools ];
  dependencies = with python3Packages; [ fastmcp mcp uvicorn limnoria tkinter ];
  nativeCheckInputs = with python3Packages; [ pytestCheckHook httpx ];
  pythonImportsCheck = [ "limnoria_mcp" "fastmcp" "tkinter" ];
}
