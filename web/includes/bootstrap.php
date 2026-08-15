<?php
declare(strict_types=1);

ini_set('display_errors', '0');

// PHP 7.4 兼容（宝塔常见版本无 str_* 系列函数）
if (!function_exists('str_starts_with')) {
    function str_starts_with(string $haystack, string $needle): bool
    {
        return $needle === '' || strncmp($haystack, $needle, strlen($needle)) === 0;
    }
}
if (!function_exists('str_contains')) {
    function str_contains(string $haystack, string $needle): bool
    {
        return $needle === '' || strpos($haystack, $needle) !== false;
    }
}
if (!function_exists('str_ends_with')) {
    function str_ends_with(string $haystack, string $needle): bool
    {
        if ($needle === '') {
            return true;
        }
        $len = strlen($needle);
        return $len <= strlen($haystack) && substr($haystack, -$len) === $needle;
    }
}

define('WEB_ROOT', dirname(__DIR__));

function web_resolve_project_root(string $webRoot): string
{
    $parent = dirname($webRoot);
    foreach ([$webRoot, $parent] as $dir) {
        if (is_file($dir . DIRECTORY_SEPARATOR . 'config.yaml')) {
            return $dir;
        }
    }
    foreach ([$webRoot, $parent] as $dir) {
        if (is_dir($dir . DIRECTORY_SEPARATOR . 'data')) {
            return $dir;
        }
    }
    $local = $webRoot . DIRECTORY_SEPARATOR . 'deploy.local.php';
    if (is_file($local)) {
        $cfg = include $local;
        if (is_array($cfg) && !empty($cfg['project_root'])) {
            $root = str_replace(['/', '\\'], DIRECTORY_SEPARATOR, (string)$cfg['project_root']);
            if (is_dir($root)) {
                return rtrim($root, DIRECTORY_SEPARATOR);
            }
        }
    }
    return $webRoot;
}

define('PROJECT_ROOT', web_resolve_project_root(WEB_ROOT));
define('DATA_DIR', PROJECT_ROOT . DIRECTORY_SEPARATOR . 'data');
define('CONFIG_PATH', PROJECT_ROOT . DIRECTORY_SEPARATOR . 'config.yaml');

function shell_available(): bool
{
    if (!function_exists('shell_exec')) {
        return false;
    }
    $disabled = ini_get('disable_functions');
    if (!is_string($disabled) || $disabled === '') {
        return true;
    }
    $list = array_map('trim', explode(',', strtolower($disabled)));
    return !in_array('shell_exec', $list, true);
}

require_once __DIR__ . '/Response.php';
require_once __DIR__ . '/Database.php';
require_once __DIR__ . '/Config.php';
require_once __DIR__ . '/Market.php';
require_once __DIR__ . '/Bridge.php';
require_once __DIR__ . '/Maintenance.php';
require_once __DIR__ . '/PnlEpoch.php';
require_once __DIR__ . '/AiGrowth.php';
require_once __DIR__ . '/IntelligenceLoop.php';

function api_token_configured(): string
{
    $cfg = Config::load();
    return trim((string)($cfg['web']['api_token'] ?? ''));
}

function bearer_token(): string
{
    $candidates = [];

    $auth = $_SERVER['HTTP_AUTHORIZATION']
        ?? $_SERVER['REDIRECT_HTTP_AUTHORIZATION']
        ?? '';
    if ($auth !== '') {
        $candidates[] = $auth;
    }

    $xToken = $_SERVER['HTTP_X_API_TOKEN'] ?? '';
    if ($xToken !== '') {
        $candidates[] = $xToken;
    }

    if (function_exists('getallheaders')) {
        $headers = getallheaders();
        if (is_array($headers)) {
            foreach ($headers as $name => $value) {
                $lower = strtolower((string)$name);
                if ($lower === 'authorization' || $lower === 'x-api-token') {
                    $candidates[] = (string)$value;
                }
            }
        }
    }

    $query = trim((string)($_GET['access_token'] ?? ''));
    if ($query !== '') {
        $candidates[] = $query;
    }

    foreach ($candidates as $raw) {
        if (preg_match('/Bearer\s+(\S+)/i', $raw, $m)) {
            return $m[1];
        }
        $plain = trim($raw);
        if ($plain !== '') {
            return $plain;
        }
    }

    return '';
}

function require_api_token(): void
{
    $token = api_token_configured();
    if ($token === '') {
        return;
    }
    $provided = bearer_token();
    if ($provided !== '' && hash_equals($token, $provided)) {
        return;
    }
    Response::error('Unauthorized — 请在 Header 携带 Authorization: Bearer <token>', 401);
}

function require_write_token(): void
{
    require_api_token();
}

header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type, Authorization, X-Api-Token');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(204);
    exit;
}
