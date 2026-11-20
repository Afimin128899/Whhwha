"""Validate and stage session uploads without extracting archive paths."""
import shutil
import stat
import zlib
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_SESSION_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 1000


class UploadError(ValueError):
    pass


def safe_parts(name):
    normalized = name.replace('\\', '/')
    path = PurePosixPath(normalized)
    if (not normalized or '\x00' in normalized or path.is_absolute()
            or '..' in path.parts or any(':' in part for part in path.parts)):
        raise UploadError('Недопустимый путь в имени файла или архиве.')
    return path


def session_filename(name):
    path = safe_parts(name)
    if len(path.parts) != 1 or not path.stem or path.suffix.lower() != '.session':
        raise UploadError('Нужно имя файла с расширением .session без пути.')
    return path.stem + '.session'


def stage_sessions(source, filename, directory):
    """Return staged files only after the complete upload has been validated."""
    source.seek(0, 2)
    if source.tell() > MAX_UPLOAD_BYTES:
        raise UploadError('Максимальный размер загрузки — 20 МБ.')
    source.seek(0)
    directory = Path(directory)
    if Path(filename).suffix.lower() == '.session':
        name = session_filename(filename)
        if source.seek(0, 2) > MAX_SESSION_BYTES:
            raise UploadError('Одна сессия не должна превышать 10 МБ.')
        source.seek(0)
        target = directory / name
        with target.open('xb') as out:
            target.chmod(0o600)
            shutil.copyfileobj(source, out)
        return [target]
    if Path(filename).suffix.lower() != '.zip':
        raise UploadError('Принимаются .session и .zip с сессиями.')
    try:
        with ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise UploadError('В ZIP должно быть не больше 1000 записей.')
            selected = []
            names = set()
            total = 0
            for info in entries:
                path = safe_parts(info.filename)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise UploadError('ZIP с символическими ссылками не поддерживается.')
                if info.is_dir() or path.suffix.lower() != '.session':
                    continue
                if '__MACOSX' in path.parts or path.name.startswith('._'):
                    continue
                name = session_filename(path.name)
                if name in names:
                    raise UploadError('В ZIP повторяются имена сессий. Переименуй файлы.')
                names.add(name)
                if info.flag_bits & 1:
                    raise UploadError('ZIP с паролем не поддерживается.')
                total += info.file_size
                if info.file_size > MAX_SESSION_BYTES or total > MAX_TOTAL_BYTES:
                    raise UploadError('Лимит: 10 МБ на сессию и 100 МБ сессий в архиве.')
                selected.append((info, directory / name))
            if not selected:
                raise UploadError('В ZIP не найдено файлов .session.')
            for info, target in selected:
                with archive.open(info) as src, target.open('xb') as out:
                    target.chmod(0o600)
                    # Reading through EOF also verifies CRC before anything is installed.
                    shutil.copyfileobj(src, out)
            return [target for _, target in selected]
    except (BadZipFile, RuntimeError, NotImplementedError, EOFError, UnicodeError, zlib.error) as exc:
        raise UploadError('ZIP повреждён, зашифрован или использует неподдерживаемое сжатие.') from exc
