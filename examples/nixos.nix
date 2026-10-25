# Import the flake's nixosModules.default in your host first.
# Illustrative only: do not activate before explicitly approving deployment,
# secret provisioning, bot-network access and service-control authorization.
{ ... }: {
  services.supybot = {
    enable = true;
    # Runtime operator-managed file, not ./secret.cfg (which enters the store).
    configFile = "/run/secrets/supybot.cfg";
  };
  services.limnoria-mcp = {
    enable = true;
    tokenFile = "/run/secrets/limnoria-mcp-token";
    jwksUrl = "https://YOUR-APPROVED-ISSUER/.well-known/jwks.json";
  };
}
