#!/usr/bin/env bash
set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
    echo "Run this installer with sudo: sudo ./install.sh" >&2
    exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install_runtime_dependencies() {
    local missing_packages=()

    command -v python3 >/dev/null 2>&1 || missing_packages+=(python3)
    command -v mpv >/dev/null 2>&1 || missing_packages+=(mpv)
    command -v chronyc >/dev/null 2>&1 || missing_packages+=(chrony)

    if [ "${#missing_packages[@]}" -eq 0 ]; then
        echo "Runtime check: Python 3, mpv, and Chrony are already installed."
        return
    fi

    echo "Missing runtime packages: ${missing_packages[*]}"

    if ! command -v apt-get >/dev/null 2>&1; then
        echo "Automatic installation requires Raspberry Pi OS, Debian, or Ubuntu." >&2
        echo "Install these packages manually: ${missing_packages[*]}" >&2
        exit 1
    fi

    echo "Updating package information..."
    apt-get update

    echo "Installing required packages..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing_packages[@]}"

    command -v python3 >/dev/null 2>&1 || {
        echo "Python 3 installation failed." >&2
        exit 1
    }
    command -v mpv >/dev/null 2>&1 || {
        echo "mpv installation failed." >&2
        exit 1
    }
}

check_system_requirements() {
    local command_name

    for command_name in systemctl getent install sed; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            echo "Required system command is missing: $command_name" >&2
            echo "This installer is intended for Raspberry Pi OS with systemd." >&2
            exit 1
        fi
    done
}

check_clock_sync() {
    local synchronized

    synchronized="$(timedatectl show --property=NTPSynchronized --value 2>/dev/null || true)"
    if [ "$synchronized" = "yes" ]; then
        echo "Clock check: network time synchronization is active."
    else
        echo "WARNING: network time synchronization is not currently confirmed."
        echo "All Pis need synchronized clocks for accurate video playback."
        echo "Check it later with: timedatectl status"
    fi
}

config_value() {
    python3 -c \
        'import configparser, sys; c=configparser.ConfigParser(); c.read(sys.argv[1]); print(c.get(sys.argv[2], sys.argv[3]))' \
        "$SOURCE_DIR/config.ini" "$1" "$2"
}

configure_chrony() {
    local manage master_device master_address allow_network hostname chrony_main chrony_fragment

    manage="$(config_value clock manage_chrony)"
    case "${manage,,}" in
        1|yes|true|on) ;;
        *)
            echo "Chrony configuration is disabled in config.ini."
            return
            ;;
    esac

    master_device="$(config_value sync master_device)"
    master_device="${master_device%%.*}"
    master_address="$(config_value clock master_address)"
    allow_network="$(config_value clock allow_network)"
    hostname="$(hostname -s)"
    chrony_main="/etc/chrony/chrony.conf"
    chrony_fragment="/etc/chrony/conf.d/video-sync.conf"

    if [ ! -f "$chrony_main" ]; then
        echo "Chrony was installed, but its configuration was not found at $chrony_main." >&2
        exit 1
    fi

    install -d -m 0755 /etc/chrony/conf.d

    # Older installations might not load conf.d automatically. Add the directive
    # once without replacing the distribution's existing Chrony configuration.
    if ! grep -Eq '^[[:space:]]*confdir[[:space:]]+/etc/chrony/conf\.d([[:space:]]|$)' "$chrony_main"; then
        printf '\n# Load project-specific configuration fragments.\nconfdir /etc/chrony/conf.d\n' >> "$chrony_main"
    fi

    if [ "${hostname,,}" = "${master_device,,}" ] || [ "${RUN_USER,,}" = "${master_device,,}" ]; then
        {
            echo "# Managed by the pi-sync installer: master clock"
            echo "allow $allow_network"
            echo "local stratum 10"
            echo "makestep 0.1 5"
        } > "$chrony_fragment"
        echo "Chrony role: master; serving time to $allow_network"
    else
        {
            echo "# Managed by the pi-sync installer: client clock"
            echo "server $master_address iburst prefer minpoll 4 maxpoll 4"
            echo "makestep 0.1 5"
        } > "$chrony_fragment"
        echo "Chrony role: client; preferred time source is $master_address"
    fi

    if command -v chronyd >/dev/null 2>&1; then
        chronyd -p >/dev/null
    fi
    systemctl enable --now chrony.service
    systemctl restart chrony.service
}

configure_login_profile() {
    local profile backup

    profile="$RUN_HOME/.bash_profile"
    backup="$RUN_HOME/.bash_profile.before-pi-sync"

    if [ -f "$profile" ] && grep -q -- '--input-ipc-server=/tmp/mpv-sync.sock' "$profile"; then
        echo "Player profile check: mpv IPC is already configured."
        return
    fi

    if [ -f "$profile" ] && grep -Eq '^[[:space:]]*ffplay([[:space:]]|$)' "$profile"; then
        if [ ! -f "$backup" ]; then
            cp -p "$profile" "$backup"
            chown "$RUN_USER:$RUN_GROUP" "$backup"
        fi

        # Replace only ffplay command lines. Keep the user's VT check, video
        # discovery, comments, and any unrelated profile customization.
        sed -i -E \
            's|^([[:space:]]*)ffplay([[:space:]].*)?$|\1exec /usr/local/bin/mpv-kiosk|' \
            "$profile"
        chown "$RUN_USER:$RUN_GROUP" "$profile"
        echo "Migrated ffplay to mpv-kiosk in $profile"
        echo "Original profile backup: $backup"
        return
    fi

    if [ -f "$profile" ] && grep -Eq '^[[:space:]]*(exec[[:space:]]+)?mpv([[:space:]]|$)' "$profile"; then
        echo "ERROR: $profile starts mpv but does not configure the required IPC socket." >&2
        echo "Add --input-ipc-server=/tmp/mpv-sync.sock to that mpv command, then rerun the installer." >&2
        exit 1
    fi

    if [ -f "$profile" ] && [ ! -f "$backup" ]; then
        cp -p "$profile" "$backup"
        chown "$RUN_USER:$RUN_GROUP" "$backup"
    fi

    {
        echo
        echo "# BEGIN pi-sync managed player"
        echo 'if [ -n "$XDG_VTNR" ] && [ "$XDG_VTNR" -eq 1 ]; then'
        echo '    exec /usr/local/bin/mpv-kiosk'
        echo 'fi'
        echo "# END pi-sync managed player"
    } >> "$profile"
    chown "$RUN_USER:$RUN_GROUP" "$profile"
    echo "Added the mpv-kiosk launcher to $profile"
}

check_system_requirements
install_runtime_dependencies

RUN_USER="${SUDO_USER:-}"
if [ -z "$RUN_USER" ] || [ "$RUN_USER" = "root" ]; then
    echo "Run with sudo from the kiosk user's account, not directly as root." >&2
    exit 1
fi
RUN_GROUP="$(id -gn "$RUN_USER")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

install -d -m 0755 /opt/video-sync /etc/video-sync /var/lib/video-sync
install -m 0755 "$SOURCE_DIR/src/mpv_sync.py" /opt/video-sync/mpv_sync.py
install -m 0755 "$SOURCE_DIR/src/mpv_control.py" /opt/video-sync/mpv_control.py
install -m 0755 "$SOURCE_DIR/bin/syncctl" /usr/local/bin/syncctl
install -m 0755 "$SOURCE_DIR/bin/mpv-kiosk" /usr/local/bin/mpv-kiosk
install -m 0644 "$SOURCE_DIR/config.ini" /etc/video-sync/config.ini
chown "$RUN_USER:$RUN_GROUP" /var/lib/video-sync

configure_chrony
configure_login_profile

sed \
    -e "s|@USER@|$RUN_USER|g" \
    -e "s|@GROUP@|$RUN_GROUP|g" \
    -e "s|@HOME@|$RUN_HOME|g" \
    "$SOURCE_DIR/systemd/video-sync.service.in" \
    > /etc/systemd/system/video-sync.service

systemctl daemon-reload
systemctl enable video-sync.service

check_clock_sync

echo "Installed for user $RUN_USER on host $(hostname)."
if [ -S /tmp/mpv-sync.sock ]; then
    systemctl restart video-sync.service
    echo "video-sync is running. Check it with: syncctl status"
else
    echo "mpv socket is not ready, so the service was not started."
    echo "Start mpv with --input-ipc-server=/tmp/mpv-sync.sock, then run: syncctl start"
fi
