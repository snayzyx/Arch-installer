# Build environment for the ArchInstall ISO.
# mkarchiso only runs on Arch, so the build happens inside this container.
# python + wimlib are only used by the offline build, to describe the bundled Windows ISO.
FROM archlinux:latest

RUN pacman -Syu --noconfirm --needed archiso python wimlib \
    && pacman -Scc --noconfirm

WORKDIR /build
