// folia-routes-sync: a Velocity plugin that keeps the proxy's backend list
// synced with folia-nexa-mgmt's live routing table (PLAN.md §7, §8C).
//
// Verified in development against the real velocity-api jar (Java 25
// bytecode — matches this project's JRE baseline elsewhere, bumped from
// 21 alongside the Velocity 3.5.1 -> 4.0.0 upgrade, see snapcraft.yaml's
// geyser-plugins part for why) using a manually-assembled classpath,
// since neither Gradle nor a JDK were preinstalled in that environment.
// This file lets a normal `./gradlew build` resolve the same
// dependencies from Maven Central/PaperMC's repo instead of hand-managed
// jars. Gradle 8.10 (this project's wrapper) can't run its own daemon on
// a JDK 25 launcher -- see the routes-sync-plugin part's build-packages
// comment in snapcraft.yaml -- so building locally needs a JDK 21 on
// JAVA_HOME with a JDK 25 also installed for toolchain auto-detection,
// e.g.:
//   JAVA_HOME=~/.local/jdk21 ./gradlew build

plugins {
    java
    id("com.gradleup.shadow") version "8.3.5"
}

group = "dev.foliasmp"
version = "0.1.0"

java {
    toolchain {
        languageVersion.set(JavaLanguageVersion.of(25))
    }
}

repositories {
    mavenCentral()
    maven("https://repo.papermc.io/repository/maven-public/") // hosts com.velocitypowered:velocity-api
}

dependencies {
    compileOnly("com.velocitypowered:velocity-api:4.0.0")
    annotationProcessor("com.velocitypowered:velocity-api:4.0.0") // generates velocity-plugin.json from @Plugin

    testImplementation(platform("org.junit:junit-bom:5.10.3"))
    testImplementation("org.junit.jupiter:junit-jupiter")
    // Gson itself is only compileOnly (transitively via velocity-api,
    // genuinely bundled by Velocity at runtime — see DisplayJson's
    // javadoc), which the test JVM doesn't get for free the way the
    // production plugin does under Velocity.
    testImplementation("com.google.code.gson:gson:2.13.2")
}

tasks.test {
    useJUnitPlatform()
}

tasks.shadowJar {
    archiveClassifier.set("") // the fat jar IS the plugin jar
}

tasks.build {
    dependsOn(tasks.shadowJar)
}
