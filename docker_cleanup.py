"""
docker_cleanup.py

Limpeza automática e periódica de imagens e volumes Docker não utilizadas em
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
import re
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional, Set

DEFAULT_LOG_FILE = Path("/var/log/docker-cleanup/docker-cleanup.log")
ANONYMOUS_VOLUME_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FRACTION_PATTERN = re.compile(r"\.\d+")

logger = logging.getLogger("docker_cleanup")


@dataclass(frozen=True)
class CleanupConfig:
    max_age_days: int
    keep_last: int
    exclude_repos: Set[str]
    exclude_tags: Set[str]
    volume_min_age_days: int
    exclude_volumes: Set[str]
    include_named_volumes: bool
    skip_volumes: bool
    dry_run: bool


@dataclass(frozen=True)
class ImageInfo:
    id: str
    repository: str
    tag: str
    created_at: Optional[datetime]

    @property
    def reference(self) -> str:
        return f"{self.repository}:{self.tag}"

    @property
    def is_tagged(self) -> bool:
        return self.repository not in ("<none>", "") and self.tag != "<none>"


@dataclass(frozen=True)
class VolumeInfo:
    name: str
    created_at: Optional[datetime]

    @property
    def is_anonymous(self) -> bool:
        return bool(ANONYMOUS_VOLUME_PATTERN.match(self.name))


def parse_timestamp(raw: str) -> Optional[datetime]:
    normalized = FRACTION_PATTERN.sub("", raw.strip()).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        logger.warning("Falha ao interpretar data '%s'.", raw)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def age_in_days(created_at: datetime, now: datetime) -> int:
    return (now - created_at).days


class CommandRunner(ABC):
    @abstractmethod
    def run(self, command: List[str]) -> str:
        raise NotImplementedError


class SubprocessRunner(CommandRunner):
    def run(self, command: List[str]) -> str:
        logger.debug("Executando: %s", " ".join(command))
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"Comando falhou ({result.returncode}): {' '.join(command)}\n"
                f"{result.stderr.strip()}"
            )
        return result.stdout


class DockerEngine:
    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def is_available(self) -> bool:
        try:
            self._runner.run(["docker", "version", "--format", "{{.Server.Version}}"])
            return True
        except Exception as exc:
            logger.error("Docker não está acessível: %s", exc)
            return False

    def container_ids(self) -> List[str]:
        output = self._runner.run(["docker", "ps", "-a", "-q"])
        return [line.strip() for line in output.splitlines() if line.strip()]

    def inspect(self, template: str, target: str) -> str:
        return self._runner.run(["docker", "inspect", "--format", template, target]).strip()

    def execute(self, command: List[str]) -> str:
        return self._runner.run(command)


class ImageGateway:
    def __init__(self, engine: DockerEngine) -> None:
        self._engine = engine

    def ids_in_use(self) -> Set[str]:
        in_use = set()
        for container_id in self._engine.container_ids():
            try:
                in_use.add(self._engine.inspect("{{.Image}}", container_id))
            except RuntimeError as exc:
                logger.warning("Não foi possível inspecionar container %s: %s", container_id, exc)
        return in_use

    def list_all(self) -> List[ImageInfo]:
        raw = self._engine.execute(
            ["docker", "image", "ls", "--no-trunc", "--format", "{{json .}}"]
        )
        entries = [json.loads(line) for line in raw.splitlines() if line.strip()]
        created = self._creation_dates({entry.get("ID", "") for entry in entries})
        return [
            ImageInfo(
                id=entry.get("ID", ""),
                repository=entry.get("Repository", ""),
                tag=entry.get("Tag", ""),
                created_at=created.get(entry.get("ID", "")),
            )
            for entry in entries
        ]

    def count_dangling(self) -> int:
        listed = self._engine.execute(["docker", "images", "-f", "dangling=true", "-q"])
        return len([line for line in listed.splitlines() if line.strip()])

    def prune_dangling(self) -> str:
        return self._engine.execute(["docker", "image", "prune", "-f"])

    def remove(self, reference: str) -> None:
        self._engine.execute(["docker", "rmi", reference])

    def _creation_dates(self, image_ids: Set[str]) -> dict:
        ids = sorted(identifier for identifier in image_ids if identifier)
        if not ids:
            return {}
        output = self._engine.execute(
            ["docker", "inspect", "--format", "{{.Id}} {{.Created}}"] + ids
        )
        dates = {}
        for line in output.splitlines():
            parts = line.strip().split(" ", 1)
            if len(parts) == 2:
                dates[parts[0]] = parse_timestamp(parts[1])
        return dates


class VolumeGateway:
    def __init__(self, engine: DockerEngine) -> None:
        self._engine = engine

    def list_unused(self) -> List[VolumeInfo]:
        output = self._engine.execute(["docker", "volume", "ls", "-q", "-f", "dangling=true"])
        names = [line.strip() for line in output.splitlines() if line.strip()]
        if not names:
            return []
        details = self._engine.execute(
            ["docker", "volume", "inspect", "--format", "{{.Name}}|{{.CreatedAt}}"] + names
        )
        volumes = []
        for line in details.splitlines():
            if "|" not in line:
                continue
            name, created = line.strip().split("|", 1)
            volumes.append(VolumeInfo(name=name, created_at=parse_timestamp(created)))
        return volumes

    def remove(self, name: str) -> None:
        self._engine.execute(["docker", "volume", "rm", name])


class ImageRetentionPolicy:
    def __init__(self, config: CleanupConfig) -> None:
        self._config = config

    def select_removable(
        self, images: List[ImageInfo], in_use: Set[str], now: datetime
    ) -> List[ImageInfo]:
        removable = []
        for repository, group in self._group_by_repository(images).items():
            if repository in self._config.exclude_repos:
                logger.info("Repositório '%s' excluído da limpeza — pulando.", repository)
                continue
            removable.extend(self._select_from_group(group, in_use, now))
        return removable

    def _group_by_repository(self, images: List[ImageInfo]) -> dict:
        grouped = {}
        for image in images:
            if image.is_tagged:
                grouped.setdefault(image.repository, []).append(image)
        return grouped

    def _select_from_group(
        self, group: List[ImageInfo], in_use: Set[str], now: datetime
    ) -> List[ImageInfo]:
        ordered = sorted(group, key=lambda image: image.created_at or now, reverse=True)
        protected_ids = {image.id for image in ordered[: self._config.keep_last]}
        return [
            image
            for image in ordered
            if self._is_removable(image, protected_ids, in_use, now)
        ]

    def _is_removable(
        self, image: ImageInfo, protected_ids: Set[str], in_use: Set[str], now: datetime
    ) -> bool:
        if image.tag in self._config.exclude_tags:
            logger.info("Tag protegida '%s' — mantendo %s.", image.tag, image.reference)
            return False
        if image.id in protected_ids:
            return False
        if image.id in in_use:
            logger.info("Imagem em uso por container — mantendo %s.", image.reference)
            return False
        if image.created_at is None:
            logger.warning("Sem data de criação para %s — mantendo por segurança.", image.reference)
            return False
        return age_in_days(image.created_at, now) >= self._config.max_age_days


class VolumeRetentionPolicy:
    def __init__(self, config: CleanupConfig) -> None:
        self._config = config

    def select_removable(self, volumes: List[VolumeInfo], now: datetime) -> List[VolumeInfo]:
        return [volume for volume in volumes if self._is_removable(volume, now)]

    def _is_removable(self, volume: VolumeInfo, now: datetime) -> bool:
        if volume.name in self._config.exclude_volumes:
            logger.info("Volume '%s' protegido — mantendo.", volume.name)
            return False
        if not volume.is_anonymous and not self._config.include_named_volumes:
            logger.info("Volume nomeado '%s' — mantendo (use --include-named-volumes).", volume.name)
            return False
        if volume.created_at is None:
            logger.warning("Sem data de criação para volume %s — mantendo por segurança.", volume.name)
            return False
        return age_in_days(volume.created_at, now) >= self._config.volume_min_age_days


class CleanupTask(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def execute(self) -> None:
        raise NotImplementedError


class DanglingImagesTask(CleanupTask):
    def __init__(self, images: ImageGateway, dry_run: bool) -> None:
        self._images = images
        self._dry_run = dry_run

    @property
    def name(self) -> str:
        return "imagens dangling"

    def execute(self) -> None:
        if self._dry_run:
            logger.info("[DRY-RUN] %d imagens dangling seriam removidas.", self._images.count_dangling())
            return
        output = self._images.prune_dangling()
        logger.info(output.strip() or "Nenhuma imagem dangling encontrada.")


class OldImagesTask(CleanupTask):
    def __init__(self, images: ImageGateway, policy: ImageRetentionPolicy, dry_run: bool) -> None:
        self._images = images
        self._policy = policy
        self._dry_run = dry_run

    @property
    def name(self) -> str:
        return "imagens antigas"

    def execute(self) -> None:
        now = datetime.now(timezone.utc)
        removable = self._policy.select_removable(
            self._images.list_all(), self._images.ids_in_use(), now
        )
        removed = sum(1 for image in removable if self._remove(image, now))
        logger.info("Resumo imagens: %d removidas de %d candidatas.", removed, len(removable))

    def _remove(self, image: ImageInfo, now: datetime) -> bool:
        age = age_in_days(image.created_at, now) if image.created_at else -1
        logger.info("Candidata à remoção: %s (idade=%dd, id=%s)", image.reference, age, image.id[:19])
        if self._dry_run:
            logger.info("[DRY-RUN] Removeria imagem: %s", image.reference)
            return True
        try:
            self._images.remove(image.reference)
            logger.info("Removida: %s", image.reference)
            return True
        except RuntimeError as exc:
            logger.warning("Falha ao remover %s: %s", image.reference, exc)
            return False


class UnusedVolumesTask(CleanupTask):
    def __init__(self, volumes: VolumeGateway, policy: VolumeRetentionPolicy, dry_run: bool) -> None:
        self._volumes = volumes
        self._policy = policy
        self._dry_run = dry_run

    @property
    def name(self) -> str:
        return "volumes não utilizados"

    def execute(self) -> None:
        now = datetime.now(timezone.utc)
        removable = self._policy.select_removable(self._volumes.list_unused(), now)
        removed = sum(1 for volume in removable if self._remove(volume))
        logger.info("Resumo volumes: %d removidos de %d candidatos.", removed, len(removable))

    def _remove(self, volume: VolumeInfo) -> bool:
        if self._dry_run:
            logger.info("[DRY-RUN] Removeria volume: %s", volume.name)
            return True
        try:
            self._volumes.remove(volume.name)
            logger.info("Volume removido: %s", volume.name)
            return True
        except RuntimeError as exc:
            logger.warning("Falha ao remover volume %s: %s", volume.name, exc)
            return False


class CleanupRunner:
    def __init__(self, tasks: List[CleanupTask]) -> None:
        self._tasks = tasks

    def run(self) -> bool:
        success = True
        for task in self._tasks:
            logger.info("Iniciando limpeza de %s...", task.name)
            try:
                task.execute()
            except Exception:
                logger.exception("Erro inesperado na limpeza de %s.", task.name)
                success = False
        return success


class TaskFactory:
    def __init__(self, engine: DockerEngine, config: CleanupConfig) -> None:
        self._engine = engine
        self._config = config

    def create_all(self) -> List[CleanupTask]:
        images = ImageGateway(self._engine)
        tasks: List[CleanupTask] = [
            DanglingImagesTask(images, self._config.dry_run),
            OldImagesTask(images, ImageRetentionPolicy(self._config), self._config.dry_run),
        ]
        if not self._config.skip_volumes:
            tasks.append(
                UnusedVolumesTask(
                    VolumeGateway(self._engine),
                    VolumeRetentionPolicy(self._config),
                    self._config.dry_run,
                )
            )
        return tasks


def configure_logging(log_file: Path, verbose: bool) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handlers = [
        RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5),
        logging.StreamHandler(sys.stdout),
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)


def split_csv(value: str) -> Set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Limpeza automática de imagens e volumes Docker.")
    parser.add_argument("--max-age-days", type=int, default=10)
    parser.add_argument("--keep-last", type=int, default=3)
    parser.add_argument("--exclude-repo", default="")
    parser.add_argument("--exclude-tag", default="latest")
    parser.add_argument("--volume-min-age-days", type=int, default=1)
    parser.add_argument("--exclude-volume", default="")
    parser.add_argument("--include-named-volumes", action="store_true")
    parser.add_argument("--skip-volumes", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG_FILE)
    parser.add_argument("--verbose", action="store_true")
    return parser


def build_config(args: argparse.Namespace) -> CleanupConfig:
    return CleanupConfig(
        max_age_days=args.max_age_days,
        keep_last=args.keep_last,
        exclude_repos=split_csv(args.exclude_repo),
        exclude_tags=split_csv(args.exclude_tag),
        volume_min_age_days=args.volume_min_age_days,
        exclude_volumes=split_csv(args.exclude_volume),
        include_named_volumes=args.include_named_volumes,
        skip_volumes=args.skip_volumes,
        dry_run=args.dry_run,
    )


def main() -> int:
    args = build_parser().parse_args()
    configure_logging(args.log_file, args.verbose)
    config = build_config(args)

    logger.info("=== Início da limpeza Docker (dry_run=%s) ===", config.dry_run)

    engine = DockerEngine(SubprocessRunner())
    if not engine.is_available():
        return 1

    tasks = TaskFactory(engine, config).create_all()
    success = CleanupRunner(tasks).run()

    logger.info("=== Limpeza concluída ===")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())