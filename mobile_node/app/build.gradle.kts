import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
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

    buildTypes {
        release {
            // No minification: the app is small, the field build must stay comparable to a
            // debug build when reading a stack trace off a phone, and there is no obfuscation
            // benefit for an APK that only ever runs on lab handsets.
            isMinifyEnabled = false
            isShrinkResources = false
            // Release stays signed with the debug keystore (~/.android/debug.keystore),
            // exactly as the Flutter build was: a signature change makes `adb install -r`
            // refuse to upgrade a field phone, and the only way past that is an uninstall,
            // which deletes its un-uploaded rescue CSVs. Whoever builds the field APK must
            // therefore keep building it on the machine that holds that keystore.
            signingConfig = signingConfigs.getByName("debug")
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
