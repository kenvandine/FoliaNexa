package dev.foliasmp.routessync;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.atomic.AtomicReference;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class MgmtRoutesClientTest {

    private HttpServer server;

    @AfterEach
    void tearDown() {
        if (server != null) {
            server.stop(0);
        }
    }

    private String start(int status, String body, AtomicReference<String> capturedAuthHeader) throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/api/v1/routes", exchange -> {
            capturedAuthHeader.set(exchange.getRequestHeaders().getFirst("Authorization"));
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(status, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        });
        server.start();
        return "http://127.0.0.1:" + server.getAddress().getPort();
    }

    @Test
    void fetchSendsBearerTokenAndParsesRoutes() throws Exception {
        AtomicReference<String> authHeader = new AtomicReference<>();
        String baseUrl = start(200, "{\"routes\": [{\"world\": \"world-lobby\", \"type\": \"lobby\", \"address\": \"10.0.2.10:25565\", \"default\": true}], \"domains\": {\"smp.example.com\": \"world-lobby\"}}", authHeader);

        MgmtRoutesClient client = new MgmtRoutesClient(baseUrl, "test-token-123", Duration.ofSeconds(5));
        MgmtRoutesClient.RoutesFetch fetched = client.fetch();
        List<Route> routes = fetched.routes();

        assertEquals("Bearer test-token-123", authHeader.get());
        assertEquals(1, routes.size());
        assertEquals("world-lobby", routes.get(0).world());
        assertTrue(routes.get(0).isDefault());
        assertEquals("world-lobby", fetched.domainRoutes().get("smp.example.com"));
    }

    @Test
    void nonTwoHundredResponseThrows() throws Exception {
        AtomicReference<String> authHeader = new AtomicReference<>();
        String baseUrl = start(503, "service unavailable", authHeader);

        MgmtRoutesClient client = new MgmtRoutesClient(baseUrl, "token", Duration.ofSeconds(5));
        assertThrows(IOException.class, client::fetch);
    }

    @Test
    void baseUrlWithTrailingSlashIsHandled() throws Exception {
        AtomicReference<String> authHeader = new AtomicReference<>();
        String baseUrl = start(200, "{\"routes\": []}", authHeader);

        MgmtRoutesClient client = new MgmtRoutesClient(baseUrl + "/", "token", Duration.ofSeconds(5));
        MgmtRoutesClient.RoutesFetch fetched = client.fetch();
        assertTrue(fetched.routes().isEmpty());
        assertTrue(fetched.domainRoutes().isEmpty());
    }
}
