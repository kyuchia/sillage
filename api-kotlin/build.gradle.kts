plugins {
    id("org.jetbrains.kotlin.jvm") version "1.9.25"
    id("com.google.devtools.ksp") version "1.9.25-1.0.20"
    id("io.micronaut.application") version "4.4.4"
}

version = "0.1"
group = "sillage"

repositories {
    mavenCentral()
}

dependencies {
    implementation("io.micronaut.kotlin:micronaut-kotlin-runtime")
    implementation("io.micronaut:micronaut-jackson-databind")
    implementation("org.jetbrains.kotlin:kotlin-reflect")
    // HikariCP directly rather than micronaut-jdbc-hikari: the pool is built from the
    // libpq-style PG_DSN the Python service already reads (SillageFactory.kt).
    implementation("com.zaxxer:HikariCP:5.1.0")
    implementation("org.postgresql:postgresql:42.7.4")
    runtimeOnly("ch.qos.logback:logback-classic")
    runtimeOnly("org.yaml:snakeyaml")
}

application {
    mainClass = "sillage.ApplicationKt"
}

java {
    sourceCompatibility = JavaVersion.toVersion("21")
    targetCompatibility = JavaVersion.toVersion("21")
}

micronaut {
    runtime("netty")
    testRuntime("junit5")
    processing {
        incremental(true)
        annotations("sillage.*")
    }
}

tasks.test {
    // Same variable the pytest suite reads; the tests refuse a database called `sillage`.
    environment("SILLAGE_TEST_DSN", System.getenv("SILLAGE_TEST_DSN") ?: "dbname=sillage_test")
    testLogging {
        events("passed", "skipped", "failed")
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
    }
}
