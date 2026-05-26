{ pkgs ? import <nixpkgs> {} }:

let
  pythonEnv = pkgs.python3.withPackages (ps: [
    ps.textual
  ]);
in
pkgs.stdenv.mkDerivation {
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

  meta = with pkgs.lib; {
    description = "Terminal UI to explore NixOS package closures and generate Nix declarations";
    homepage = "https://github.com/manelinux/nixard";
    license = licenses.mit;
    platforms = platforms.linux;
    mainProgram = "nixard";
  };
}
