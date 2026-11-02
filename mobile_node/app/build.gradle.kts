import org.jetbrains.kotlin.gradle.dsl.JvmTarget
import java.io.File
import java.io.FileInputStream
import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

// ── release signing ────────────────────────────────────────────────────────────
// The field phones get ONE uninstall/reinstall (upload the rescue CSVs, then
// `adb uninstall`, then install). From then on every release build must carry the same
// dedicated key, so back the .jks up: a different signature again means another
// uninstall, and Android deletes the phone-local rescue data on uninstall.
//
// Source order: mobile_node/keystore.properties first, then environment variables (for a
// CI/one-off build). A missing key is not fatal: the build falls back to the debug key,
// prints a loud warning and marks the app version "-debugsigned" so the dashboard shows it.
val keystorePropertiesFile = rootProject.file("keystore.properties")
val keystoreProperties = Properties().apply {
    if (keystorePropertiesFile.isFile) {
        FileInputStream(keystorePropertiesFile).use { load(it) }
    }
}

fun signingValue(property: String, env: String): String? =
    keystoreProperties.getProperty(property)?.takeIf { it.isNotBlank() }
        ?: System.getenv(env)?.takeIf { it.isNotBlank() }

val releaseStorePath = signingValue("storeFile", "IMU_KEYSTORE_FILE")
val releaseStorePassword = signingValue("storePassword", "IMU_KEYSTORE_PASSWORD")
val releaseKeyAlias = signingValue("keyAlias", "IMU_KEY_ALIAS")
val releaseKeyPassword = signingValue("keyPassword", "IMU_KEY_PASSWORD")
// storeFile is relative to mobile_node/ (the root project) unless absolute.
val releaseStoreFile: File? = releaseStorePath?.let {
    val candidate = File(it)
    if (candidate.isAbsolute) candidate else rootProject.file(it)
}
val releaseSigningReady = releaseStoreFile != null &&
    releaseStoreFile.isFile &&
    releaseStorePassword != null &&
    releaseKeyAlias != null &&
    releaseKeyPassword != null
val releaseSigningProblem: String? = when {
    releaseStorePath == null && releaseStorePassword == null &&
        releaseKeyAlias == null && releaseKeyPassword == null -> null // nothing configured
    !releaseSigningReady ->
        "release signing is incomplete or the keystore does not exist (" +
            (releaseStoreFile?.path ?: "no storeFile") + ")"
    else -> null
}

android {
    namespace = "com.example.sensors_app"
    // compileSdk 35 because the build laptop has platforms 35/36/37 only; targetSdk stays 34,
    // the level the field phones were validated at, so runtime behaviour does not change.
    compileSdk = 35

    defaultConfig {
        // Same applicationId as the Flutter build: the new APK installs OVER the old one and
        // keeps its data, which is how device_id and the local rescue CSVs survive (PHONE.md).
        applicationId = "com.example.sensors_app"
        minSdk = 26
        targetSdk = 34
        versionCode = 5
        versionName = "3.0.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildFeatures {
        compose = true
        // BuildConfig.VERSION_NAME is the only version string the UI shows; it is never
        // hard-coded, so a phone reporting 3.0.0 on the dashboard really is this build.
        buildConfig = true
    }

    signingConfigs {
        if (releaseSigningReady) {
            create("release") {
                storeFile = releaseStoreFile
                storePassword = releaseStorePassword
                keyAlias = releaseKeyAlias
                keyPassword = releaseKeyPassword
            }
        }
    }

    buildTypes {
        release {
            // No minification: the app is small, the field build must stay comparable to a
            // debug build when reading a stack trace off a phone, and there is no obfuscation
            // benefit for an APK that only ever runs on lab handsets.
            isMinifyEnabled = false
            isShrinkResources = false
            signingConfig = if (releaseSigningReady) {
                signingConfigs.getByName("release")
            } else {
                // Do not fail an ordinary developer build, but make the fallback impossible
                // to miss and visible on the dashboard through the version string.
                logger.warn(
                    "release APK is signed with this machine's DEBUG key — NOT FOR FIELD PHONES",
                )
                releaseSigningProblem?.let {
                    logger.warn("configured $it — falling back to the debug key")
                }
                versionNameSuffix = "-debugsigned"
                signingConfigs.getByName("debug")
            }
        }
        debug {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlin {
        compilerOptions {
            jvmTarget.set(JvmTarget.JVM_17)
        }
    }

    sourceSets {
        getByName("main") {
            java.srcDirs("src/main/kotlin")
        }
        getByName("test") {
            java.srcDirs("src/test/kotlin")
        }
    }

    testOptions {
        unitTests {
            // Robolectric needs real (merged) Android resources for the Compose UI tests.
            isIncludeAndroidResources = true
            isReturnDefaultValues = true
        }
    }

    lint {
        abortOnError = true
        warningsAsErrors = false
        checkReleaseBuilds = true
        textReport = true
    }

    packaging {
        resources {
            excludes += setOf(
                "/META-INF/{AL2.0,LGPL2.1}",
                "/META-INF/DEPENDENCIES",
                "/META-INF/LICENSE*",
                "/META-INF/NOTICE*",
            )
        }
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.runtime.compose)

    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)

    implementation(libs.okhttp)
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.kotlinx.serialization.json)

    testImplementation(libs.junit)
    testImplementation(libs.robolectric)
    testImplementation(libs.androidx.test.core)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(platform(libs.androidx.compose.bom))
    testImplementation(libs.androidx.compose.ui.test.junit4)
    // Declares androidx.activity.ComponentActivity in the debug manifest so the Robolectric
    // Compose rules have an activity to host the screens under test.
    debugImplementation(libs.androidx.compose.ui.test.manifest)

    debugImplementation(libs.androidx.compose.ui.tooling)
}
