#!/bin/sh
# Install the Slurm collector and the Zabbix agent UserParameters.
#
# Run on the host that has the Slurm client commands and the Zabbix agent:
#
#   sudo ./install.sh                 # agent, direct collection
#   sudo ./install.sh --timer         # agent 2 / agent, cache refreshed by systemd
#   sudo ./install.sh --accounting    # also collect job throughput from sacct
#
# The Zabbix template itself is imported through the Zabbix frontend, see
# README.md.

set -eu

SOURCE_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)

BIN_TARGET=/usr/local/bin/slurm_zabbix.py
CACHE_DIR=/var/lib/zabbix-slurm
CACHE_FILE="$CACHE_DIR/cache.json"
ZABBIX_USER=zabbix
USE_TIMER=0
USE_ACCOUNTING=0

usage() {
    cat <<EOF
Usage: $0 [--timer] [--accounting] [--user USER] [--agent-dir DIR]

  --timer        install the systemd timer and configure the agent to read the
                 cache only (recommended above a few hundred nodes)
  --accounting   also install the slurm.accounting UserParameter, which reads
                 job throughput from sacct. The matching item still has to be
                 enabled on the host in Zabbix.
  --user USER    account the Zabbix agent runs as (default: $ZABBIX_USER)
  --agent-dir    agent include directory; autodetected when not given
EOF
}

AGENT_DIR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --timer) USE_TIMER=1 ;;
        --accounting) USE_ACCOUNTING=1 ;;
        --user) ZABBIX_USER=$2; shift ;;
        --agent-dir) AGENT_DIR=$2; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ "$(id -u)" -ne 0 ]; then
    echo "This script needs to run as root." >&2
    exit 1
fi

# --- locate the agent include directory ------------------------------------
# Zabbix agent 2 ships "Include=/etc/zabbix/zabbix_agent2.d/plugins.d/*.conf",
# so a UserParameter file dropped straight into zabbix_agent2.d is never read.
if [ -z "$AGENT_DIR" ]; then
    if [ -d /etc/zabbix/zabbix_agent2.d ]; then
        AGENT_DIR=/etc/zabbix/zabbix_agent2.d/plugins.d
    else
        for candidate in /etc/zabbix/zabbix_agentd.d /etc/zabbix/zabbix_agentd.conf.d \
                         /usr/local/etc/zabbix_agentd.conf.d; do
            if [ -d "$candidate" ]; then
                AGENT_DIR=$candidate
                break
            fi
        done
    fi
fi

if [ -z "$AGENT_DIR" ]; then
    echo "Could not find a Zabbix agent include directory; pass --agent-dir." >&2
    exit 1
fi

if [ ! -d "$AGENT_DIR" ]; then
    echo "Creating $AGENT_DIR"
    mkdir -p "$AGENT_DIR"
fi

# Warn when the agent does not actually include the directory we are about to
# write to: the UserParameters would load silently nowhere and every item would
# come back as "Unsupported item key".
for agent_conf in /etc/zabbix/zabbix_agent2.conf /etc/zabbix/zabbix_agentd.conf \
                  /usr/local/etc/zabbix_agentd.conf; do
    [ -f "$agent_conf" ] || continue
    if ! grep -qs "^[[:space:]]*Include=.*$AGENT_DIR" "$agent_conf"; then
        echo "WARNING: $agent_conf has no Include covering $AGENT_DIR." >&2
        echo "         Add:  Include=$AGENT_DIR/*.conf" >&2
    fi
done

# --- sanity checks ----------------------------------------------------------
missing=""
for command in scontrol squeue sdiag; do
    command -v "$command" >/dev/null 2>&1 || missing="$missing $command"
done
if [ -n "$missing" ]; then
    echo "WARNING: Slurm commands not found in PATH:$missing" >&2
    echo "         The collector will report errors until they are available." >&2
fi

if ! id "$ZABBIX_USER" >/dev/null 2>&1; then
    echo "WARNING: user $ZABBIX_USER does not exist; adjust with --user." >&2
fi

# --- collector --------------------------------------------------------------
echo "Installing collector to $BIN_TARGET"
install -m 0755 "$SOURCE_DIR/bin/slurm_zabbix.py" "$BIN_TARGET"

echo "Creating cache directory $CACHE_DIR"
install -d -m 0750 -o "$ZABBIX_USER" -g "$ZABBIX_USER" "$CACHE_DIR" 2>/dev/null || \
    install -d -m 0750 "$CACHE_DIR"

# --- agent configuration ----------------------------------------------------
CONF_TARGET="$AGENT_DIR/slurm.conf"
echo "Installing UserParameters to $CONF_TARGET"
if [ "$USE_TIMER" -eq 1 ]; then
    cat > "$CONF_TARGET" <<EOF
# Installed by Zabbix-Slurm install.sh --timer
# The cache is refreshed by zabbix-slurm-collector.timer.
UserParameter=slurm.cluster,$BIN_TARGET --mode cluster --cache-file $CACHE_FILE --cache-only
UserParameter=slurm.nodes,$BIN_TARGET --mode nodes --cache-file $CACHE_FILE --cache-only
EOF
else
    cat > "$CONF_TARGET" <<EOF
# Installed by Zabbix-Slurm install.sh
# Requires Timeout=30 in the agent configuration.
UserParameter=slurm.cluster,$BIN_TARGET --mode cluster --cache-file $CACHE_FILE --cache-ttl 55
UserParameter=slurm.nodes,$BIN_TARGET --mode nodes --cache-file $CACHE_FILE --cache-ttl 55
EOF
fi
if [ "$USE_ACCOUNTING" -eq 1 ]; then
    if [ "$USE_TIMER" -eq 1 ]; then
        ACCOUNTING_ARGS="--cache-only"
    else
        ACCOUNTING_ARGS="--accounting-window 3600"
    fi
    cat >> "$CONF_TARGET" <<EOF
UserParameter=slurm.accounting,$BIN_TARGET --mode accounting --cache-file $CACHE_FILE $ACCOUNTING_ARGS
EOF
fi
chmod 0644 "$CONF_TARGET"

# --- systemd timer ----------------------------------------------------------
if [ "$USE_TIMER" -eq 1 ]; then
    echo "Installing systemd units"
    install -m 0644 "$SOURCE_DIR/systemd/zabbix-slurm-collector.service" \
        /etc/systemd/system/zabbix-slurm-collector.service
    install -m 0644 "$SOURCE_DIR/systemd/zabbix-slurm-collector.timer" \
        /etc/systemd/system/zabbix-slurm-collector.timer
    if [ "$USE_ACCOUNTING" -eq 1 ]; then
        # A second ExecStart refreshes the separately cached accounting data.
        mkdir -p /etc/systemd/system/zabbix-slurm-collector.service.d
        cat > /etc/systemd/system/zabbix-slurm-collector.service.d/accounting.conf <<EOF
[Service]
ExecStart=$BIN_TARGET --refresh --mode accounting --cache-file $CACHE_FILE
EOF
    fi
    systemctl daemon-reload
    systemctl enable --now zabbix-slurm-collector.timer
    echo "Priming the cache"
    systemctl start zabbix-slurm-collector.service || \
        echo "WARNING: the first collection failed, check: journalctl -u zabbix-slurm-collector" >&2
fi

# --- verification -----------------------------------------------------------
echo ""
echo "Verifying collection as $ZABBIX_USER"
if su -s /bin/sh -c "$BIN_TARGET --mode cluster --cache-file $CACHE_FILE --pretty" \
        "$ZABBIX_USER" 2>/dev/null | head -20; then
    :
else
    echo "WARNING: the collector could not be run as $ZABBIX_USER." >&2
fi

cat <<EOF

Done.

Next steps:
  1. Set Timeout=30 in the agent configuration if you did not use --timer.
  2. Restart the agent:   systemctl restart zabbix-agent2   (or zabbix-agent)
  3. Check from the Zabbix server:
       zabbix_get -s <host> -k slurm.cluster
  4. Import templates/slurm_cluster_7.0.xml in the Zabbix frontend and link it
     to the host.
EOF

if [ "$USE_ACCOUNTING" -eq 1 ]; then
    cat <<EOF
  5. Enable the "Slurm: Get accounting data" item on the host: it ships
     disabled, and every accounting metric depends on it.
EOF
fi
