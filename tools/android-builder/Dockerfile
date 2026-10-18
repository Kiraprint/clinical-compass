# Локальная сборка Android APK (item 4). JDK 21 + Android SDK 36.
# На машине нет Java/SDK/gradle — собираем в этом образе через CLI.
# Использование: tools/build-apk.sh
FROM eclipse-temurin:21-jdk

ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends unzip curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Android command-line tools
RUN mkdir -p $ANDROID_HOME/cmdline-tools && cd $ANDROID_HOME/cmdline-tools \
 && curl -fsSL https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip -o cmdtools.zip \
 && unzip -q cmdtools.zip && mv cmdline-tools latest && rm cmdtools.zip

ENV PATH=$PATH:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools

# Лицензии + platform 36 / build-tools 36 (compileSdk/targetSdk = 36, variables.gradle)
RUN yes | sdkmanager --licenses >/dev/null 2>&1 || true
RUN sdkmanager "platforms;android-36" "build-tools;36.0.0" "platform-tools"
