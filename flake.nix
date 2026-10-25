{
  description = "Run an omp agent unattended, streaming its thinking into the job log";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (
        pkgs:
        let
          omp-agent = pkgs.writeShellApplication {
            name = "omp-agent";
            runtimeInputs = [ pkgs.python3 ];
            text = ''
              exec python3 ${./omp_agent.py} "$@"
            '';
            meta = {
              description = "Run an omp agent unattended, with its thinking streamed into the job log";
              mainProgram = "omp-agent";
              platforms = pkgs.lib.platforms.all;
            };
          };
        in
        {
          inherit omp-agent;
          default = omp-agent;
        }
      );

      # The action and the command are the same code, so the suite covers both
      # halves: the argument mapping in action.sh and the runner itself.
      checks = forAllSystems (pkgs: {
        tests = pkgs.runCommand "omp-agent-tests" { nativeBuildInputs = [ pkgs.python3 ]; } ''
          cp -r ${self} source
          chmod -R u+w source
          cd source
          python3 -m unittest discover -s tests -v
          touch $out
        '';
      });
    };
}
