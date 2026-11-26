#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "$0")" && pwd)
role=$(<"$root/role.txt")
platform=$(<"$root/platform.txt")
arch="${platform#linux/}"
case "$role" in a|b|c) ;; *) echo "Invalid bundle role: $role" >&2; exit 2 ;; esac
case "$platform" in linux/arm64|linux/amd64) ;; *) echo "Invalid bundle platform" >&2; exit 2 ;; esac

if [[ -n "${2:-}" ]]; then
    # Source a deployment operator's local configuration.
    source "$2"
fi
name="${CE_NAME:-clusterenergy-$role}"
mode="${CE_MODE:-real}"
storage="${CE_STORAGE:-mysql}"
certs="${CE_CERTS_DIR:-$root/certs}"
config="${CE_CONFIG_DIR:-$root/config}"
data="${CE_DATA_DIR:-$name-data}"
interval="${CE_INTERVAL:-5}"
cert_mount=(--mount "type=bind,source=$certs,target=/certs,readonly")
state_mount=(-v "$data:/data")
network=(--network host)
common=("${network[@]}" --restart unless-stopped --init --pull never)

mysql_host="${CE_MYSQL_HOST:-127.0.0.1}"
mysql_port="${CE_MYSQL_PORT:-3306}"
mysql_user="${CE_MYSQL_USER:-clusterenergy}"
mysql_password="${CE_MYSQL_PASSWORD:-clusterenergy123}"
mysql_root_password="${CE_MYSQL_ROOT_PASSWORD:-$mysql_password}"
mysql_db="${CE_MYSQL_DB:-clusterenergy_$mode}"
mysql_data="${CE_MYSQL_DATA_DIR:-$name-mysql-data}"
mysql_managed="${CE_MYSQL_MANAGED:-}"
if [[ -z "$mysql_managed" ]]; then
    if [[ "$storage" == mysql && ( "$mysql_host" == "127.0.0.1" || "$mysql_host" == "localhost" ) ]]; then
        mysql_managed=true
    else
        mysql_managed=false
    fi
fi

if [[ "$role" == b ]]; then
    if [[ "$storage" == mysql && "$mysql_managed" == true ]]; then
        images=(mysql gateway web)
        names=("$name-mysql" "$name-gateway" "$name-web")
    else
        images=(gateway web)
        names=("$name-gateway" "$name-web")
    fi
else
    images=("$role")
    names=("$name")
fi

ensure_images() {
    local server image
    server=$(docker info --format '{{.OSType}}/{{.Architecture}}')
    case "$server" in
        linux/aarch64) server=linux/arm64 ;;
        linux/x86_64) server=linux/amd64 ;;
    esac
    if [[ "$server" != "$platform" ]]; then
        echo "Bundle requires $platform, Docker server reports $server" >&2
        exit 1
    fi
    for image in "${images[@]}"; do
        if [[ "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "clusterenergy/$image:$arch" 2>/dev/null || true)" != "$platform" ]]; then
            if [[ ! -r "$root/images/$image.tar" ]]; then
                echo "Missing image archive: $root/images/$image.tar" >&2
                exit 1
            fi
            docker load -i "$root/images/$image.tar"
        fi
        if [[ "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "clusterenergy/$image:$arch")" != "$platform" ]]; then
            echo "Loaded image has an unexpected platform: $image" >&2
            exit 1
        fi
    done
}

remove_service() {
    local instance
    local targets=("${names[@]}")
    if [[ "$role" == b ]]; then
        targets=("$name-web" "$name-gateway" "$name-mysql")
    fi
    for instance in "${targets[@]}"; do
        if docker container inspect "$instance" >/dev/null 2>&1; then
            docker rm -f "$instance" >/dev/null
        fi
    done
}

require_files() {
    local filename
    for filename in "$@"; do
        if [[ ! -r "$filename" ]]; then
            echo "Missing file: $filename" >&2
            exit 1
        fi
    done
}

wait_for_mysql() {
    local container="$1"
    local attempts=90
    echo "Waiting for MySQL container $container on 127.0.0.1:$mysql_port ..."
    for ((i = 1; i <= attempts; i++)); do
        if docker exec "$container" mysqladmin ping -h 127.0.0.1 -P "$mysql_port" -u root "-p$mysql_root_password" --silent >/dev/null 2>&1; then
            echo "MySQL is ready."
            return 0
        fi
        sleep 1
    done
    echo "MySQL container $container did not become ready within ${attempts}s" >&2
    docker logs --tail 30 "$container" >&2 || true
    return 1
}

case "${1:-start}" in
    status)
        for instance in "${names[@]}"; do
            docker container inspect --format '{{.Name}} {{.State.Status}}' "$instance"
        done
        exit 0 ;;
    stop) remove_service; exit 0 ;;
    load) ensure_images; exit 0 ;;
    start|restart) ;;
    *) echo "Usage: bash deploy.sh [start|restart|stop|status|load] [config.env]" >&2; exit 2 ;;
esac

if [[ "$mode" != real && "$mode" != simulated ]]; then
    echo "CE_MODE must be real or simulated" >&2
    exit 2
fi
if [[ "$role" == b && "$storage" != sqlite && "$storage" != mysql ]]; then
    echo "CE_STORAGE must be sqlite or mysql" >&2
    exit 2
fi
require_files "$certs/ca.crt"
mounts=()
args=()
gateway_env=()
if [[ "$role" == a ]]; then
    require_files "$certs/a_server.crt" "$certs/a_server.key"
    args=(--bind "${CE_BIND:-0.0.0.0}" --port "${CE_A_PORT:-19200}" --interval "$interval"
          --db "/data/a-$mode.sqlite" --retention-days "${CE_A_RETENTION_DAYS:-15}"
          --ca /certs/ca.crt --server-cert /certs/a_server.crt --server-key /certs/a_server.key)
    if [[ "$mode" == simulated ]]; then
        args+=(--sim-nodes "${CE_SIM_NODES:-8}")
    else
        require_files "$certs/a_client.crt" "$certs/a_client.key"
        if [[ -z "${CE_C_TARGETS:-}" ]]; then
            echo "Set CE_C_TARGETS to a comma-separated name=https://host:port list" >&2
            exit 2
        fi
        IFS=, read -r -a targets <<< "$CE_C_TARGETS"
        args+=(--client-cert /certs/a_client.crt --client-key /certs/a_client.key)
        for target in "${targets[@]}"; do args+=(--target "$target"); done
    fi
    if [[ -r "$config/racks.json" ]]; then
        mounts+=(--mount "type=bind,source=$config,target=/config,readonly")
        args+=(--racks /config/racks.json)
    fi
elif [[ "$role" == c ]]; then
    source_dir="${CE_SOURCE_DIR:?Set CE_SOURCE_DIR to the directory updated by the local collector}"
    if [[ ! -d "$source_dir" ]]; then echo "Missing source directory: $source_dir" >&2; exit 1; fi
    require_files "$certs/c_server.crt" "$certs/c_server.key"
    mounts+=(--mount "type=bind,source=$source_dir,target=/source,readonly")
    args=(--bind "${CE_BIND:-0.0.0.0}" --port "${CE_C_PORT:-19100}" --interval "$interval"
          --node-id "${CE_C_ID:-$(hostname -s)}" --data-file "/source/${CE_SOURCE_FILE:-latest.json}" --db /data/c-replay.sqlite
          --retention-days "${CE_C_RETENTION_DAYS:-1}" --ca /certs/ca.crt --cert /certs/c_server.crt --key /certs/c_server.key)
    if [[ -r "$config/schema.json" ]]; then
        mounts+=(--mount "type=bind,source=$config,target=/config,readonly")
        args+=(--schema /config/schema.json)
    fi
else
    require_files "$certs/b_client.crt" "$certs/b_client.key"
    upstream="${CE_A_UPSTREAM:?Set CE_A_UPSTREAM to the A HTTPS endpoint}"
    args=(--bind 127.0.0.1 --port "${CE_GATEWAY_PORT:-18081}" --upstream "$upstream"
          --ca /certs/ca.crt --cert /certs/b_client.crt --key /certs/b_client.key --mode "$mode" --interval "$interval"
          --storage "$storage" --db "/data/b-$mode.sqlite"
          --retention-days "${CE_B_RETENTION_DAYS:-30}" --hot-days "${CE_B_HOT_DAYS:-30}")
    if [[ "$storage" == mysql ]]; then
        args+=(--mysql-host "$mysql_host" --mysql-port "$mysql_port"
               --mysql-user "$mysql_user" --mysql-db "$mysql_db")
        gateway_env+=(-e "CE_MYSQL_PASSWORD=$mysql_password")
    fi
    if [[ -n "${CE_SIM_CLOCK_OFFSET_SECONDS:-}" ]]; then
        args+=(--sim-clock-offset-seconds "$CE_SIM_CLOCK_OFFSET_SECONDS")
    fi
    if [[ -n "${CE_JOBS_DIR:-}" ]]; then
        mounts+=(--mount "type=bind,source=$CE_JOBS_DIR,target=/jobs,readonly")
        args+=(--jobs-file /jobs/jobs.json)
    fi
fi
ensure_images
remove_service
if [[ "$role" == b ]]; then
    if [[ "$storage" == mysql && "$mysql_managed" == true ]]; then
        docker run -d --name "$name-mysql" "${common[@]}" \
            -v "$mysql_data:/var/lib/mysql" \
            -e "MYSQL_ROOT_PASSWORD=$mysql_root_password" \
            -e "MYSQL_DATABASE=$mysql_db" \
            -e "MYSQL_USER=$mysql_user" \
            -e "MYSQL_PASSWORD=$mysql_password" \
            "clusterenergy/mysql:$arch" \
            --bind-address=127.0.0.1 --port="$mysql_port" \
            --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci
        if ! wait_for_mysql "$name-mysql"; then
            remove_service
            exit 1
        fi
    fi
    if ! docker run -d --name "$name-gateway" "${common[@]}" "${cert_mount[@]}" \
        "${state_mount[@]}" "${mounts[@]}" "${gateway_env[@]}" \
        "clusterenergy/gateway:$arch" "${args[@]}"; then
        remove_service
        exit 1
    fi
    if ! docker run -d --name "$name-web" "${common[@]}" \
        -e "CE_GATEWAY_ADDR=127.0.0.1:${CE_GATEWAY_PORT:-18081}" -e "CE_WEB_PORT=${CE_WEB_PORT:-18088}" \
        "clusterenergy/web:$arch"; then
        remove_service
        exit 1
    fi
else
    docker run -d --name "$name" "${common[@]}" "${cert_mount[@]}" "${state_mount[@]}" \
        "${mounts[@]}" "clusterenergy/$role:$arch" "${args[@]}"
fi
