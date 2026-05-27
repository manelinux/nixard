{
  description = "nixard — terminal UI to explore NixOS package closures";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        pythonEnv = pkgs.python3.withPackages (ps: [
          ps.textual
        ]);
      in
      {
        packages.default = pkgs.stdenv.mkDerivation {
          pname = "nixard";
          version = "2.0.0";

          src = ./.;

          nativeBuildInputs = [ pkgs.makeWrapper ];
          buildInputs = [ pythonEnv ];

          installPhase = ''
            mkdir -p $out/bin $out/share/nixard
            cp nixard.py $out/share/nixard/nixard.py
            makeWrapper ${pythonEnv}/bin/python3 $out/bin/nixard \
              --add-flags "$out/share/nixard/nixard.py"
          '';

          meta = {
            description = "Terminal UI to explore NixOS package closures and generate Nix declarations";
            homepage = "https://github.com/manelinux/nixard";
            license = pkgs.lib.licenses.mit;
            maintainers = [ ];
            platforms = pkgs.lib.platforms.unix;
            mainProgram = "nixard";
          };
        };

        apps.default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/nixard";
        };

        devShells.default = pkgs.mkShell {
          packages = [ pythonEnv ];
        };
      }
    );
}
