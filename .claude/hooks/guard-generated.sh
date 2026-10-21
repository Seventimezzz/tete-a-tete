#!/usr/bin/env bash
# PreToolUse для Edit/Write: запрещает ручную правку сгенерированных файлов протокола.
# Источник истины — server/app/protocol.py, файлы пересоздаёт `npm run gen:protocol`.

input=$(cat)
path=$(printf '%s' "$input" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -n 1 | sed 's/^.*:[[:space:]]*"//; s/"$//')
path=${path//\\\\//}
path=${path//\\//}

case "$path" in
  */protocol/schema.json | */client/src/protocol.gen.ts)
    echo "Файл $path генерируется. Измените модель в server/app/protocol.py и запустите npm run gen:protocol (скилл protocol-change)." >&2
    exit 2
    ;;
esac
exit 0
