#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3}"

service_required() {
    if [[ "$(id -u)" != 0 ]] || ! command -v systemctl >/dev/null; then
        echo '此操作需要在使用 systemd 的 Linux 上以 root 执行。' >&2
        exit 1
    fi
}

run_action() {
    case "${1:-}" in
        install|start|restart)
            service_required
            "$PYTHON" deploy/operations.py check
            if [[ "$1" == install ]]; then
                "$PYTHON" deploy/operations.py install
                systemctl daemon-reload
                systemctl enable cpbot.service
                systemctl restart cpbot.service
            else
                systemctl "$1" cpbot.service
            fi
            sleep 3
            systemctl is-active --quiet cpbot.service || { journalctl -u cpbot -n 30 --no-pager; return 1; }
            echo 'cpbot 服务进程已启动；使用日志查看 Telegram 连接情况。'
            ;;
        stop) service_required; systemctl stop cpbot.service ;;
        status) service_required; systemctl status cpbot.service --no-pager ;;
        logs) service_required; journalctl -u cpbot.service -n 80 -f ;;
        backup) "$PYTHON" deploy/operations.py backup ;;
        update)
            service_required
            "$PYTHON" deploy/operations.py update
            ;;
        upgrade)
            service_required
            [[ -n "${2:-}" ]] || { echo '用法：bash manage.sh upgrade /opt/cpbot-new'; return 1; }
            "$PYTHON" deploy/operations.py upgrade "$2"
            ;;
        run) "$PYTHON" -m cpbot ;;
        *) echo '用法：bash manage.sh [install|start|stop|restart|status|logs|backup|update|upgrade 新版目录|run]'; return 1 ;;
    esac
}

if [[ $# -gt 0 ]]; then
    run_action "$@"
else
    while true; do
        printf '\ncpbot 管理\n1) 安装 / 更新服务并启动\n2) 启动\n3) 停止\n4) 重启\n5) 状态\n6) 日志（Ctrl+C 退出）\n7) 备份\n8) 从新版目录升级\n9) 从 Git 更新当前目录\n0) 退出\n'
        read -r -p '请选择：' choice || exit 0
        case "$choice" in
            1) run_action install ;;
            2) run_action start ;;
            3) run_action stop ;;
            4) run_action restart ;;
            5) run_action status || true ;;
            6) run_action logs ;;
            7) run_action backup ;;
            8) read -r -p '新版目录：' source; run_action upgrade "$source" ;;
            9) run_action update ;;
            0) exit 0 ;;
            *) echo '请选择菜单中的数字。' ;;
        esac
    done
fi
