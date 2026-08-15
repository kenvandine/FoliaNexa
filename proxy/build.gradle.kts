// folia-routes-sync: a Velocity plugin that keeps the proxy's backend list
// synced with folia-nexa-mgmt's live routing table (PLAN.md §7, §8C).
//
// Verified in development against the real velocity-api 3.5.1 jar (Java
// 21 bytecode — matches this project's JRE baseline elsewhere) using a
// manually-assembled classpath, since neither Gradle nor a JDK were
// preinstalled in that environment. This file lets a normal `./gradlew
// build` resolve the same dependencies from Maven Central/PaperMC's repo
// instead of hand-managed jars.

plugins {
    java
    id("com.gradleup.shadow") version "8.3.5"
}

group = "dev.foliasmp"
version = "0.1.0"

java {
    toolchain {
        languageVersion.set(JavaLanguageVersion.of(21))
    }
}

repositories {
    mavenCentral()
    maven("https://repo.papermc.io/repository/maven-public/") // hosts com.velocitypowered:velocity-api
}

dependencies {
    compileOnly("com.velocitypowered:velocity-api:3.5.1")
    annotationProcessor("com.velocitypowered:velocity-api:3.5.1") // generates velocity-plugin.json from @Plugin

    testImplementation(platform("org.junit:junit-bom:5.10.3"))
    testImplementation("org.junit.jupiter:junit-jupiter")
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
