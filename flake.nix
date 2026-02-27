{
    description = "srcg-core";

    inputs = {
        nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    };

    outputs = { self, nixpkgs }: {
        devShells.default = nixpkgs.legacyPackages.x86_64-linux.mkShell {
            packages = [
                nixpkgs.legacyPackages.x86_64-linux.cmake
                nixpkgs.legacyPackages.x86_64-linux.make
                nixpkgs.legacyPackages.x86_64-linux.gcc
            ];
        };
    };
}