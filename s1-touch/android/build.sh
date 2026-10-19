#!/usr/bin/env bash
# Baut S1Mobile.apk ohne Gradle/Android Studio.
# Benötigt (Debian/Ubuntu): apt install android-sdk-platform-23 aapt zipalign apksigner dalvik-exchange openjdk-17-jdk-headless
set -euo pipefail
cd "$(dirname "$0")"
SDK=/usr/lib/android-sdk
JAR=$(ls $SDK/platforms/android-*/android.jar | sort -V | tail -1)
BT=$(ls -d $SDK/build-tools/*/ | sort -V | tail -1)
OUT=build; rm -rf $OUT; mkdir -p $OUT/gen $OUT/classes
VER=${VERSION:-1.0}; CODE=${VERSION_CODE:-1}
sed -e "s/android:versionCode=\"[0-9]*\"/android:versionCode=\"$CODE\"/" -e "s/android:versionName=\"[^\"]*\"/android:versionName=\"$VER\"/" AndroidManifest.xml > $OUT/AndroidManifest.xml
aapt package -f -m -J $OUT/gen -M $OUT/AndroidManifest.xml -S res -I "$JAR" -F $OUT/app.unsigned.apk
if ! javac -nowarn -source 8 -target 8 -bootclasspath "$JAR" -classpath "$JAR" -d $OUT/classes $OUT/gen/de/s1mobile/app/R.java src/de/s1mobile/app/*.java > $OUT/javac.log 2>&1; then
  grep -v JAVA_TOOL_OPTIONS $OUT/javac.log; echo "Java-Kompilierung fehlgeschlagen"; exit 1; fi
"$BT/dx" --dex --min-sdk-version=23 --output=$OUT/classes.dex $OUT/classes
(cd $OUT && aapt add -f app.unsigned.apk classes.dex >/dev/null)
zipalign -f 4 $OUT/app.unsigned.apk $OUT/app.aligned.apk
KS=s1mobile.keystore
[ -f $KS ] || keytool -genkeypair -keystore $KS -alias s1mobile -storepass s1mobile -keypass s1mobile -keyalg RSA -keysize 2048 -validity 20000 -dname "CN=S1 Mobile, O=Privat, C=DE" >/dev/null 2>&1
apksigner sign --ks $KS --ks-pass pass:s1mobile --key-pass pass:s1mobile --out S1Mobile.apk $OUT/app.aligned.apk
apksigner verify --print-certs S1Mobile.apk | head -2
echo "Fertig: $(pwd)/S1Mobile.apk ($(du -h S1Mobile.apk | cut -f1))"
