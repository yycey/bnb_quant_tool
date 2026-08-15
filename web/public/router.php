<?php
/**
 * PHP built-in server router: /api → api/index.php；静态文件原样返回。
 */
$uri = urldecode(parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/');
$file = __DIR__ . $uri;
if ($uri !== '/' && is_file($file)) {
    return false;
}
if ($uri === '/' || $uri === '' || $uri === '/index.html') {
    return false; // 交给内置服务器提供 index.html
}
if (strpos($uri, '/api') === 0) {
    require __DIR__ . '/api/index.php';
    return true;
}
http_response_code(404);
header('Content-Type: text/plain; charset=utf-8');
echo 'Not Found';
return true;
