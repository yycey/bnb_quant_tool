<?php
declare(strict_types=1);

final class Response
{
    public static function json($data, int $code = 200): void
    {
        http_response_code($code);
        header('Content-Type: application/json; charset=utf-8');
        $flags = JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES;
        if (defined('JSON_INVALID_UTF8_SUBSTITUTE')) {
            $flags |= JSON_INVALID_UTF8_SUBSTITUTE;
        }
        $body = json_encode($data, $flags);
        if ($body === false) {
            $body = json_encode(['error' => 'JSON encode failed'], $flags) ?: '{"error":"JSON encode failed"}';
        }
        echo $body;
        exit;
    }

    public static function error(string $message, int $code = 400): void
    {
        $message = trim($message);
        if ($message === '') {
            $message = 'Internal server error';
        }
        self::json(['error' => $message], $code);
    }
}
