plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.vortex.driver"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.vortex.driver"
        minSdk = 26          // Android 8.0 -- foreground-service notifications + adaptive icons
        targetSdk = 34       // Android 14
        versionCode = 24
        versionName = "0.21.0-b11.4"
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
            // Suffix so debug + release can coexist on one device.
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
        }
        release {
            isMinifyEnabled = false   // turn on once we ship for real
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true   // generates Kt classes for layouts; safer than findViewById
        // AGP 8.x doesn't generate BuildConfig unless explicitly enabled --
        // and we reference BuildConfig.VERSION_NAME from MainActivity.
        buildConfig = true
    }

    // CI (GitHub Actions) signs debug builds with the auto-generated debug keystore;
    // release builds are unsigned for now and we'll wire signing in M4.
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.lifecycle:lifecycle-service:2.8.7")

    // Kotlin coroutines for the socket-poll loop.
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")

    // B1: OkHttp for the outbound WebSocket to the hub + the enroll
    // POST. Standard, ~600 KB, includes WebSocket out of the box.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // B3: Java-WebSocket for the inbound direct-WS server (browser <->
    // APK direct, no hub in the data path). ~200 KB, no transitive deps,
    // mature WebSocketServer base class with onOpen/onClose/onMessage.
    implementation("org.java-websocket:Java-WebSocket:1.5.7")
}
