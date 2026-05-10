// Settings for the Vortex Driver Android project.
//
// This is a separate Gradle root inside the larger Vortex repo so the
// Python tree (hub/, agent/) and the Kotlin tree (driver/) don't share
// build state. Open driver/ in Android Studio if you want IDE support.

pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "VortexDriver"
include(":app")
