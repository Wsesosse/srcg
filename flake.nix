{
  description = "srcg-core: cgroup wrapper for memory restricted execution";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in
      {
        packages.default = pkgs.stdenv.mkDerivation {
          pname = "srcg-core";
          version = "0.1.0";
          src = ./.;

          nativeBuildInputs = [ pkgs.cmake ];
          
          installPhase = ''
            mkdir -p $out/bin
            cp srcg $out/bin/srcg
          '';
        };

        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            cmake
            gnumake
            gcc
          ];
        };
      }
    );
}