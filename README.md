# srcg-core — Dependencies

## Runtime Requirements

* Linux with **cgroup v2**
* Root privileges (`sudo`)

Check cgroup v2:

```bash
mount | grep cgroup2
```

---

## Build Dependencies

### Required

* C++ compiler (one of)

  * `gcc` (>= 10)
  * `clang`
* `cmake` (>= 3.16)
* `make` or `ninja`

---

## Install (Common Distros)

### Arch Linux

```bash
sudo pacman -S base-devel cmake
```

### Ubuntu / Debian

```bash
sudo apt install build-essential cmake
```

### Fedora

```bash
sudo dnf groupinstall "Development Tools"
sudo dnf install cmake
```

---

## Build

```bash
cmake -B build
cmake --build build
```

Binary:

```
build/srcg
```

Optional install:

```bash
sudo cp build/srcg /usr/local/bin/
```
