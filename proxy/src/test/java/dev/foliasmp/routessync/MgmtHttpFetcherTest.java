package dev.foliasmp.routessync;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.time.Duration;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotSame;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * Covers the self-healing behavior added after a real incident
 * (2026-08-17, play.sullivan.linuxgroove.com): a pooled connection that
 * goes silently black-holed wedges the JDK HttpClient until something
 * rebuilds it, since the JDK itself won't do that on its own short of a
 * clean connection failure.
 */
class MgmtHttpFetcherTest {

    private HttpServer server;
    private ServerSocket blackHole;

    @AfterEach
    void tearDown() throws IOException {
        if (server != null) {
            server.stop(0);
        }
        if (blackHole != null) {
            blackHole.close();
        }
    }

    private String startBlackHole() throws IOException {
        // Accepts connections but never responds and never closes them —
        // every request against it will hang until its own timeout fires,
        // the same symptom a wedged pooled connection produces.
        blackHole = new ServerSocket(0, 0, java.net.InetAddress.getByName("127.0.0.1"));
        Thread acceptor = new Thread(() -> {
            while (!blackHole.isClosed()) {
                try {
                    Socket ignored = blackHole.accept();
                    // deliberately never read/write/close it
                } catch (IOException e) {
                    return;
                }
            }
        });
        acceptor.setDaemon(true);
        acceptor.start();
        return "http://127.0.0.1:" + blackHole.getLocalPort();
    }

    private String startWorkingServer(int status, String body) throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/api/v1/routes", exchange -> {
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(status, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        });
        server.start();
        return "http://127.0.0.1:" + server.getAddress().getPort();
    }

    @Test
    void rebuildsClientAfterThreeConsecutiveFailures() throws Exception {
        String baseUrl = startBlackHole();
        MgmtHttpFetcher fetcher = new MgmtHttpFetcher(baseUrl, "token", Duration.ofMillis(200));

        HttpClient original = fetcher.currentClient();

        assertThrows(IOException.class, () -> fetcher.get("/api/v1/routes"));
        assertSame(original, fetcher.currentClient(), "one failure shouldn't trigger a rebuild yet");

        assertThrows(IOException.class, () -> fetcher.get("/api/v1/routes"));
        assertSame(original, fetcher.currentClient(), "two failures shouldn't trigger a rebuild yet");

        assertThrows(IOException.class, () -> fetcher.get("/api/v1/routes"));
        assertNotSame(original, fetcher.currentClient(), "a third consecutive failure should rebuild the client");
    }

    @Test
    void aSuccessResetsTheFailureStreak() throws Exception {
        String blackHoleUrl = startBlackHole();
        MgmtHttpFetcher fetcher = new MgmtHttpFetcher(blackHoleUrl, "token", Duration.ofMillis(200));
        HttpClient original = fetcher.currentClient();

        assertThrows(IOException.class, () -> fetcher.get("/api/v1/routes"));
        assertThrows(IOException.class, () -> fetcher.get("/api/v1/routes"));
        assertSame(original, fetcher.currentClient());

        // A working server on a different port stands in for the failing
        // one recovering mid-streak — two failures shouldn't carry over.
        tearDown();
        String workingUrl = startWorkingServer(200, "{\"routes\": []}");
        MgmtHttpFetcher recovered = new MgmtHttpFetcher(workingUrl, "token", Duration.ofSeconds(5));
        assertEquals("{\"routes\": []}", recovered.get("/api/v1/routes"));
    }

    @Test
    void nonTwoHundredResponseDoesNotCountAsAConnectionFailure() throws Exception {
        String baseUrl = startWorkingServer(503, "service unavailable");
        MgmtHttpFetcher fetcher = new MgmtHttpFetcher(baseUrl, "token", Duration.ofSeconds(5));
        HttpClient original = fetcher.currentClient();

        for (int i = 0; i < 5; i++) {
            assertThrows(IOException.class, () -> fetcher.get("/api/v1/routes"));
        }

        assertSame(original, fetcher.currentClient(), "a real (if unhappy) HTTP response proves the connection works");
    }
}
