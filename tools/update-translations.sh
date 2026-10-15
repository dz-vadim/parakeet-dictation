#!/usr/bin/env bash
# Refresh the Ukrainian catalogue: extract _() strings, merge, compile.
# Needs gettext (xgettext, msgmerge, msgfmt).  Commit the .po AND the .mo.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
pot=$(mktemp --suffix=.pot)
trap 'rm -f "$pot"' EXIT
find parakeet_dictation -name '*.py' | sort | xargs xgettext \
    --language=Python --keyword=_ --keyword=ngettext:1,2 --from-code=UTF-8 \
    --package-name=parakeet-dictation --output="$pot"
po=parakeet_dictation/locale/uk/LC_MESSAGES/parakeet-dictation.po
mkdir -p "$(dirname "$po")"
if [ -f "$po" ]; then
    msgmerge --quiet --update --no-fuzzy-matching --backup=none "$po" "$pot"
else
    msginit --no-translator --locale=uk_UA.UTF-8 --input="$pot" --output-file="$po"
fi
msgattrib --no-obsolete --output-file="$po" "$po"
msgfmt --check --output-file="${po%.po}.mo" "$po"
untranslated=$(msgattrib --untranslated "$po" | grep -c '^msgid ' || true)
echo "untranslated entries (incl. header): $untranslated"
