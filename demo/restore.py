"""Restore the confirmed demo into a new, confined project directory."""
import json
import stat
import uuid
import sys
import zipfile
from pathlib import Path, PurePosixPath

from product_ai_agent.experience import set_active, session
from product_ai_agent.storage import Store, project_path, fail, no_links
from product_ai_agent.workflow import validation

archive=Path(__file__).with_name('confirmed-run.zip')
if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
root=project_path('projects/local-saved-demo-'+uuid.uuid4().hex[:8])
with zipfile.ZipFile(archive) as z:
    entries=z.infolist()
    if len(entries)>2000 or sum(e.file_size for e in entries)>64*1024*1024:
        fail('LIMIT_EXCEEDED','Демонстрационный архив превышает допустимый размер.')
    prepared=[]
    for entry in entries:
        path=PurePosixPath(entry.filename)
        if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or ':' in entry.filename or len(path.parts)<2:
            fail('ACCESS_DENIED','Недопустимый путь архива.')
        if stat.S_ISLNK(entry.external_attr>>16):fail('ACCESS_DENIED','Ссылки в архиве запрещены.')
        rel=PurePosixPath(*path.parts[1:]).as_posix()
        if not entry.is_dir():prepared.append((rel,z.read(entry)))
root.mkdir(parents=True)
store=Store(root)
for rel,payload in prepared:
    target=store.path(rel);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(payload)
report=validation(store)
if not report['valid']:fail('VALIDATION_ERROR','Восстановленный пример не прошёл проверку: '+str(report['errors']))
set_active(root)
print(json.dumps({'ok':True,'project':root.relative_to(Path.cwd()).as_posix(),'session':session(str(root))},ensure_ascii=False))
