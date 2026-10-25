{ python3Packages }:
python3Packages.buildPythonPackage {
  pname = "limnoria-mcp";
  version = "0.1.0";
  pyproject = true;
  src = ../.;
  build-system = [ python3Packages.setuptools ];
  dependencies = with python3Packages; [ fastmcp mcp uvicorn limnoria ];
  nativeCheckInputs = with python3Packages; [ pytestCheckHook httpx ];
  pythonImportsCheck = [ "limnoria_mcp" "fastmcp" ];
}
