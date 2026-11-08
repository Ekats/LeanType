#!/bin/sh
# Builds the app's native engine (app/src/main/jni) as a host library and the Java probe.
# Usage: tools/dictbuild/probe/build.sh   (from the repo root; needs clang++ and a JDK)
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
JNI="$ROOT/app/src/main/jni"
JH="${JAVA_HOME:-$(dirname "$(dirname "$(readlink -f "$(which javac)")")")}"
OUT="$HERE/out"; mkdir -p "$OUT/obj"
for f in "$JNI"/*.cpp $(find "$JNI/src" -name '*.cpp'); do
  o="$OUT/obj/$(echo "$f" | sed "s#$JNI/##" | tr / _).o"
  echo "clang++ -std=c++17 -O2 -fPIC -w -I $JNI/src -I $JH/include -I $JH/include/linux -c $f -o $o"
done | xargs -P "$(nproc)" -I{} sh -c '{}'
clang++ -shared -o "$OUT/libjni_latinime.so" "$OUT"/obj/*.o
javac -d "$OUT" $(find "$HERE" -name '*.java')
echo "built $OUT (run: java -Dlatinime.lib=$OUT/libjni_latinime.so -cp $OUT Probe <dict> <locale> <mode> ...)"
