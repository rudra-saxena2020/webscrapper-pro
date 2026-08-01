{pkgs}: {
  deps = [
    pkgs.chromium
    pkgs.wayland
    pkgs.libdrm
    pkgs.systemd
    pkgs.expat
    pkgs.glib
    pkgs.cairo
    pkgs.pango
    pkgs.xorg.libXext
    pkgs.xorg.libXrandr
    pkgs.xorg.libX11
    pkgs.at-spi2-core
    pkgs.alsa-lib
    pkgs.libxkbcommon
    pkgs.xorg.libxcb
    pkgs.mesa
    pkgs.xorg.libXfixes
    pkgs.xorg.libXdamage
    pkgs.xorg.libXcomposite
    pkgs.dbus
    pkgs.at-spi2-atk
    pkgs.atk
    pkgs.nss
    pkgs.nspr
  ];
}
