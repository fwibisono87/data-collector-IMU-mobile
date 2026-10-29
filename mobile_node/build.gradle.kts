// Root build file for the native Android IMU node.
// Plugin versions come from gradle/libs.versions.toml; nothing is configured here so the
// single :app module stays the only place a build change can be made.
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.kotlin.compose) apply false
    alias(libs.plugins.kotlin.serialization) apply false
}
