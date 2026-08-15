<?php
declare(strict_types=1);

final class Market
{
    private const SPOT_MIRRORS = [
        'https://api.binance.me',
        'https://data-api.binance.vision',
        'https://api.binance.com',
    ];

    public static function snapshot(string $symbol = 'BNBUSDT'): array
    {
        $result = [
            'price' => 0,
            'change_24h' => 0,
            'funding_rate' => 0,
            'fear_greed' => 0,
            'source' => 'none',
        ];

        self::mergePythonSnapshot($result, $symbol);
        if ($result['price'] > 0) {
            return $result;
        }

        $ticker = self::fetchSpotJson('/api/v3/ticker/24hr', ['symbol' => $symbol]);
        if ($ticker) {
            $result['price'] = (float)($ticker['lastPrice'] ?? 0);
            $result['change_24h'] = (float)($ticker['priceChangePercent'] ?? 0);
            $result['source'] = 'php_binance';
        }

        if ($result['price'] <= 0) {
            $mexc = self::fetchMexcTicker($symbol);
            if ($mexc) {
                $result['price'] = (float)($mexc['price'] ?? 0);
                $result['change_24h'] = (float)($mexc['change_24h'] ?? 0);
                $result['source'] = 'php_mexc';
            }
        }

        if ($result['fear_greed'] <= 0) {
            $fng = self::httpGet('https://api.alternative.me/fng/?limit=1');
            if ($fng) {
                $data = json_decode($fng, true);
                $value = $data['data'][0]['value'] ?? null;
                if ($value !== null) {
                    $result['fear_greed'] = (int)$value;
                }
            }
        }

        if ($result['funding_rate'] == 0) {
            $result['funding_rate'] = self::fetchFundingRate($symbol);
        }

        return $result;
    }

    public static function lastPrice(string $symbol = 'BNBUSDT'): float
    {
        $snap = self::snapshot($symbol);
        $price = (float)($snap['price'] ?? 0);
        return $price > 0 ? $price : 0.0;
    }

    /** @param array<string, mixed> $result */
    private static function mergePythonSnapshot(array &$result, string $symbol): void
    {
        $bridge = Bridge::marketSnapshot($symbol);
        if (!$bridge || ($bridge['price'] ?? 0) <= 0) {
            return;
        }

        $result['price'] = (float)$bridge['price'];
        $result['change_24h'] = (float)($bridge['change_24h'] ?? 0);
        $result['funding_rate'] = (float)($bridge['funding_rate'] ?? 0);
        $result['fear_greed'] = (int)($bridge['fear_greed'] ?? 0);
        $result['source'] = 'python';
    }

    private static function fetchFundingRate(string $symbol): float
    {
        $gateSymbol = str_replace('USDT', '_USDT', $symbol);
        $gate = self::httpGet(
            'https://api.gateio.ws/api/v4/futures/usdt/contracts/' . rawurlencode($gateSymbol)
        );
        if ($gate) {
            $data = json_decode($gate, true);
            if (is_array($data) && isset($data['funding_rate'])) {
                return round((float)$data['funding_rate'] * 100, 4);
            }
        }

        $premium = self::fetchSpotJson('/fapi/v1/premiumIndex', ['symbol' => $symbol], true);
        if ($premium && isset($premium['lastFundingRate'])) {
            return round((float)$premium['lastFundingRate'] * 100, 4);
        }

        return 0.0;
    }

    /** @return array{price: float, change_24h: float}|null */
    private static function fetchMexcTicker(string $symbol): ?array
    {
        $body = self::httpGet('https://api.mexc.com/api/v3/ticker/24hr?symbol=' . rawurlencode($symbol));
        if (!$body) {
            return null;
        }
        $json = json_decode($body, true);
        if (!is_array($json)) {
            return null;
        }
        $price = (float)($json['lastPrice'] ?? 0);
        if ($price <= 0) {
            return null;
        }
        return [
            'price' => $price,
            'change_24h' => (float)($json['priceChangePercent'] ?? 0),
        ];
    }

    private static function fetchSpotJson(string $path, array $params = [], bool $futures = false): ?array
    {
        $query = http_build_query($params);
        $bases = $futures
            ? ['https://fapi.binance.com', 'https://fapi.binance.me']
            : self::SPOT_MIRRORS;

        foreach ($bases as $base) {
            $body = self::httpGet($base . $path . '?' . $query);
            if (!$body) {
                continue;
            }
            $json = json_decode($body, true);
            if (is_array($json)) {
                return $json;
            }
        }
        return null;
    }

    private static function httpGet(string $url): ?string
    {
        if (function_exists('curl_init')) {
            $ch = curl_init($url);
            curl_setopt_array($ch, [
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_TIMEOUT => 8,
                CURLOPT_CONNECTTIMEOUT => 5,
                CURLOPT_FOLLOWLOCATION => true,
                CURLOPT_SSL_VERIFYPEER => false,
                CURLOPT_SSL_VERIFYHOST => 0,
                CURLOPT_HTTPHEADER => ['Accept: application/json'],
                CURLOPT_USERAGENT => 'Mozilla/5.0 (compatible; BNBQuantWeb/1.0)',
            ]);
            $body = curl_exec($ch);
            $code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
            curl_close($ch);
            if ($body !== false && $code >= 200 && $code < 300) {
                return $body;
            }
            return null;
        }

        $ctx = stream_context_create([
            'http' => [
                'method' => 'GET',
                'timeout' => 8,
                'header' => "Accept: application/json\r\nUser-Agent: BNBQuantWeb/1.0\r\n",
            ],
            'ssl' => [
                'verify_peer' => false,
                'verify_peer_name' => false,
            ],
        ]);
        $body = @file_get_contents($url, false, $ctx);
        return $body === false ? null : $body;
    }
}
