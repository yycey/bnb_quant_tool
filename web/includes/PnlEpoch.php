<?php
declare(strict_types=1);

/**
 * 累计盈亏统计周期 — 不删交易记录，仅让 Web 仪表盘从指定时间起重新累计
 */
final class PnlEpoch
{
    private static function filePath(): string
    {
        return DATA_DIR . DIRECTORY_SEPARATOR . 'pnl_epoch.json';
    }

    public static function load(): array
    {
        $path = self::filePath();
        if (!is_file($path)) {
            return [];
        }
        $data = json_decode((string)file_get_contents($path), true);
        return is_array($data) ? $data : [];
    }

    public static function epochStart(): ?string
    {
        $start = trim((string)(self::load()['epoch_start'] ?? ''));
        return $start !== '' ? $start : null;
    }

    /** @return array{sql: string, params: list<string>} */
    public static function closedFilter(): array
    {
        $start = self::epochStart();
        if ($start === null) {
            return ['sql' => '', 'params' => []];
        }
        return ['sql' => ' AND closed_at >= ?', 'params' => [$start]];
    }

    public static function info(): array
    {
        $meta = self::load();
        $start = self::epochStart();
        return [
            'epoch_start' => $start,
            'active' => $start !== null,
            'set_at' => $meta['set_at'] ?? null,
            'note' => $meta['note'] ?? null,
            'label' => $start !== null
                ? ('自 ' . self::formatLabel($start) . ' 起累计')
                : '全部历史',
        ];
    }

    public static function resetFromToday(): array
    {
        $start = date('Y-m-d') . 'T00:00:00';
        return self::save($start, '从今天 00:00 重新累计盈亏（历史交易保留供 AI 学习）');
    }

    public static function resetFromNow(): array
    {
        return self::save(date('Y-m-d\TH:i:s'), '从当前时刻重新累计盈亏（历史交易保留供 AI 学习）');
    }

    public static function clear(): array
    {
        $path = self::filePath();
        if (is_file($path)) {
            @unlink($path);
        }
        return [
            'ok' => true,
            'epoch_start' => null,
            'active' => false,
            'message' => '已恢复为全部历史统计',
        ];
    }

    private static function save(string $epochStart, string $note): array
    {
        if (!is_dir(DATA_DIR)) {
            @mkdir(DATA_DIR, 0755, true);
        }
        $payload = [
            'epoch_start' => $epochStart,
            'set_at' => date('c'),
            'note' => $note,
        ];
        $path = self::filePath();
        if (file_put_contents($path, json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT)) === false) {
            return ['ok' => false, 'error' => '无法写入 data/pnl_epoch.json，请检查目录权限'];
        }
        return array_merge(['ok' => true], self::info(), ['message' => $note]);
    }

    private static function formatLabel(string $iso): string
    {
        $ts = strtotime($iso);
        if ($ts === false) {
            return $iso;
        }
        return date('Y-m-d H:i', $ts);
    }
}
