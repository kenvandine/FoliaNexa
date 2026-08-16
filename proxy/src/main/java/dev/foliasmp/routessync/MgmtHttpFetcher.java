package dev.foliasmp.routessync;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;

/**
 * Shared bearer-token HTTP client for talking to folia-nexa-mgmt — GET for
 * polling ({@link MgmtRoutesClient} §7, {@link AccessGateClient} §11C,
 * {@link MgmtDisplayClient}, chat/pending), POST for reporting
 * (chat/report, §16).
 */
final class MgmtHttpFetcher {
    private final HttpClient http;
    private final String baseUrl;
    private final String apiToken;
    private final Duration timeout;

    MgmtHttpFetcher(String baseUrl, String apiToken, Duration timeout) {
        this.http = HttpClient.newBuilder().connectTimeout(timeout).build();
        this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        this.apiToken = apiToken;
        this.timeout = timeout;
    }

    String get(String path) throws IOException, InterruptedException {
        HttpRequest request = HttpRequest.newBuilder(URI.create(baseUrl + path))
                .header("Authorization", "Bearer " + apiToken)
                .timeout(timeout)
                .GET()
                .build();

        HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 200) {
            throw new IOException("mgmt returned HTTP " + response.statusCode() + " for " + path);
        }
        return response.body();
    }

    void post(String path, String jsonBody) throws IOException, InterruptedException {
        HttpRequest request = HttpRequest.newBuilder(URI.create(baseUrl + path))
                .header("Authorization", "Bearer " + apiToken)
                .header("Content-Type", "application/json")
                .timeout(timeout)
                .POST(HttpRequest.BodyPublishers.ofString(jsonBody, StandardCharsets.UTF_8))
                .build();

        HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 200) {
            throw new IOException("mgmt returned HTTP " + response.statusCode() + " for " + path);
        }
    }
}
