"""
docker_cleanup.py

Limpeza automática e periódica de imagens Docker não utilizadas em
servidores de CI/CD que fazem build de imagens com frequência.

Estratégia:
  1. Remove imagens "dangling" (<none>:<none>) — sempre seguro.
  2. Para cada repositório (ex: registry/app), mantém as N tags mais
     recentes (--keep-last) e remove as demais, DESDE QUE mais antigas
     que --max-age-days.
  3. Nunca remove uma imagem que esteja em uso por um container
     (rodando ou parado).
  4. Suporta --dry-run para simular sem apagar nada.
  5. Loga tudo em arquivo (com rotação simples) e no stdout.

Uso:
  python3 docker_cleanup.py --max-age-days 10 --keep-last 3
  python3 docker_cleanup.py --dry-run
  python3 docker_cleanup.py --exclude-repo minhaorg/app-critica --exclude-tag latest,prod

Requisitos:
  - Docker CLI disponível no PATH e permissão para rodar `docker` (root
    ou usuário no grupo `docker`).
  - Python 3.8+ (só usa biblioteca padrão, sem dependências externas).
"""

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_LOG_DIR = Path("/var/log/docker-cleanup")
DEFAULT_LOG_FILE = DEFAULT_LOG_DIR / "docker-cleanup.log"

logger = logging.getLogger("docker_cleanup")


def setup_logging(log_file: Path, verbose: bool) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)


def run(cmd: list) -> str:
    """Executa um comando e retorna stdout. Lança exceção em erro."""
    logger.debug("Executando: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Comando falhou ({result.returncode}): {' '.join(cmd)}\n{result.stderr.strip()}"
        )
    return result.stdout


def docker_available() -> bool:
    try:
        run(["docker", "version", "--format", "{{.Server.Version}}"])
        return True
    except Exception as exc:  
        logger.error("Docker não está acessível: %s", exc)
        return False


def get_images_in_use() -> set:
    """
    Retorna o conjunto de Image IDs (sha256 completo) usados por QUALQUER
    container, incluindo parados — para nunca remover algo que ainda
    pode ser reiniciado.
    """
    output = run(["docker", "ps", "-a", "-q"])
    container_ids = [c for c in output.splitlines() if c.strip()]

    in_use = set()
    for cid in container_ids:
        try:
            image_id = run(["docker", "inspect", "--format", "{{.Image}}", cid]).strip()
            in_use.add(image_id)
        except RuntimeError as exc:
            logger.warning("Não foi possível inspecionar container %s: %s", cid, exc)
    return in_use


def get_all_images() -> list:
    """
    Retorna lista de dicts com metadata de cada imagem:
      { id, repository, tag, created_at (datetime UTC) }
    Usa `docker image ls` + `docker inspect` para timestamp preciso.
    """
    raw = run(
        [
            "docker",
            "image",
            "ls",
            "--no-trunc",
            "--format",
            "{{json .}}",
        ]
    )

    images = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        image_id = data.get("ID", "")
        repo = data.get("Repository", "")
        tag = data.get("Tag", "")
        images.append({"id": image_id, "repository": repo, "tag": tag})

    
    unique_ids = sorted({img["id"] for img in images})
    created_map = {}
    if unique_ids:
        inspect_out = run(
            ["docker", "inspect", "--format", "{{.Id}} {{.Created}}"] + unique_ids
        )
        for line in inspect_out.splitlines():
            if not line.strip():
                continue
            parts = line.split(" ", 1)
            if len(parts) != 2:
                continue
            full_id, created_str = parts
            try:
                created_dt = datetime.fromisoformat(
                    created_str.replace("Z", "+00:00").split(".")[0] + "+00:00"
                    if "." in created_str
                    else created_str.replace("Z", "+00:00")
                )
            except ValueError:
                logger.warning(
                    "Falha ao parsear data de criação para %s: '%s'", full_id, created_str
                )
                created_dt = None
            created_map[full_id] = created_dt

    for img in images:
        img["created_at"] = created_map.get(img["id"])

    return images


def remove_image(image_ref: str, dry_run: bool) -> bool:
    if dry_run:
        logger.info("[DRY-RUN] Removeria imagem: %s", image_ref)
        return True
    try:
        run(["docker", "rmi", image_ref])
        logger.info("Removida: %s", image_ref)
        return True
    except RuntimeError as exc:
        logger.warning("Falha ao remover %s (provavelmente em uso/dependência): %s", image_ref, exc)
        return False


def prune_dangling(dry_run: bool) -> int:
    """Remove imagens <none>:<none> (dangling). Sempre seguro."""
    logger.info("Removendo imagens dangling (<none>:<none>)...")
    cmd = ["docker", "image", "prune", "-f"]
    if dry_run:
        cmd = ["docker", "image", "prune", "-f", "--filter", "dangling=true"]
        
        listed = run(["docker", "images", "-f", "dangling=true", "-q"])
        count = len([x for x in listed.splitlines() if x.strip()])
        logger.info("[DRY-RUN] %d imagens dangling seriam removidas.", count)
        return count

    output = run(cmd)
    logger.info(output.strip() or "Nenhuma imagem dangling encontrada.")
    
    return output.count("\n")


def cleanup_old_images(
    max_age_days: int,
    keep_last: int,
    exclude_repos: set,
    exclude_tags: set,
    dry_run: bool,
) -> None:
    in_use = get_images_in_use()
    images = get_all_images()

    now = datetime.now(timezone.utc)

    
    by_repo = {}
    for img in images:
        if img["repository"] in ("<none>", "") or img["tag"] == "<none>":
            continue
        by_repo.setdefault(img["repository"], []).append(img)

    total_removed = 0
    total_kept_by_policy = 0

    for repo, imgs in by_repo.items():
        if repo in exclude_repos:
            logger.info("Repositório '%s' excluído da limpeza — pulando.", repo)
            continue

        
        imgs_sorted = sorted(
            imgs,
            key=lambda i: i["created_at"] or now,
            reverse=True,
        )

        
        keep_set = {img["id"] for img in imgs_sorted[:keep_last]}

        for img in imgs_sorted:
            ref = f"{img['repository']}:{img['tag']}"

            if img["tag"] in exclude_tags:
                logger.info("Tag protegida '%s' — mantendo %s.", img["tag"], ref)
                continue

            if img["id"] in keep_set:
                total_kept_by_policy += 1
                continue

            if img["id"] in in_use:
                logger.info("Imagem em uso por container — mantendo %s.", ref)
                continue

            age_days = None
            if img["created_at"]:
                age_days = (now - img["created_at"]).days

            if age_days is None:
                logger.warning("Sem data de criação para %s — mantendo por segurança.", ref)
                continue

            if age_days < max_age_days:
                continue  

            logger.info(
                "Candidata à remoção: %s (idade=%dd, id=%s)", ref, age_days, img["id"][:19]
            )
            if remove_image(ref, dry_run):
                total_removed += 1

    logger.info(
        "Resumo: %d imagens removidas, %d mantidas por política de retenção (keep-last=%d).",
        total_removed,
        total_kept_by_policy,
        keep_last,
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Limpeza automática de imagens Docker.")
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=10,
        help="Idade mínima (em dias) para uma imagem ser candidata à remoção (default: 10).",
    )
    parser.add_argument(
        "--keep-last",
        type=int,
        default=3,
        help="Quantidade mínima de tags mais recentes a manter por repositório (default: 3).",
    )
    parser.add_argument(
        "--exclude-repo",
        default="",
        help="Lista de repositórios a nunca limpar, separados por vírgula.",
    )
    parser.add_argument(
        "--exclude-tag",
        default="latest",
        help="Lista de tags a nunca remover, separadas por vírgula (default: latest).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Apenas simula, não remove nada.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=DEFAULT_LOG_FILE,
        help=f"Arquivo de log (default: {DEFAULT_LOG_FILE}).",
    )
    parser.add_argument("--verbose", action="store_true", help="Log em nível DEBUG.")
    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging(args.log_file, args.verbose)

    logger.info("=== Início da limpeza de imagens Docker (dry_run=%s) ===", args.dry_run)

    if not docker_available():
        sys.exit(1)

    exclude_repos = {r.strip() for r in args.exclude_repo.split(",") if r.strip()}
    exclude_tags = {t.strip() for t in args.exclude_tag.split(",") if t.strip()}

    try:
        prune_dangling(args.dry_run)
        cleanup_old_images(
            max_age_days=args.max_age_days,
            keep_last=args.keep_last,
            exclude_repos=exclude_repos,
            exclude_tags=exclude_tags,
            dry_run=args.dry_run,
        )
    except Exception:
        logger.exception("Erro inesperado durante a limpeza.")
        sys.exit(1)

    logger.info("=== Limpeza concluída ===")


if __name__ == "__main__":
    main()
