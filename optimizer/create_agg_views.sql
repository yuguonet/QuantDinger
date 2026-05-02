SET client_encoding TO 'UTF8';

DO $$
DECLARE
    tbl TEXT;
    tz INT := 28800;

    tables_15m TEXT[];
    tables_1d  TEXT[];

    parts_30m TEXT[] := '{}';
    parts_1h  TEXT[] := '{}';
    parts_2h  TEXT[] := '{}';
    parts_1d  TEXT[] := '{}';
    parts_1w  TEXT[] := '{}';
    sql_text TEXT;
BEGIN

    SELECT array_agg(table_name ORDER BY table_name)
    INTO tables_15m
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_type = 'BASE TABLE'
      AND table_name LIKE 'kline\_15m\_%'
      AND table_name NOT LIKE 'kline\_1d\_%'
      AND table_name NOT LIKE 'kline\_30m\_%'
      AND table_name NOT LIKE 'kline\_1h\_%'
      AND table_name NOT LIKE 'kline\_2h\_%'
      AND table_name NOT LIKE 'kline\_1w\_%';


    SELECT array_agg(table_name ORDER BY table_name)
    INTO tables_1d
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_type = 'BASE TABLE'
      AND table_name LIKE 'kline\_1D\_%'
      AND table_name NOT LIKE 'kline\_1w\_%';


    IF tables_15m IS NULL OR array_length(tables_15m, 1) = 0 THEN
        RAISE NOTICE ' kline_15m_*  unfind, skip 30m/1h/2h/1D VIEW creation';
        RETURN;
    END IF;

    -- ========================================================
    -- 从 15m --> 30m / 1h / 2h / 1D 的 UNION
    -- ========================================================
    FOREACH tbl IN ARRAY tables_15m LOOP
        -- 30m: bucket = time - time % 1800
        parts_30m := array_append(parts_30m, format($f$
            SELECT symbol, time - time %% 1800 AS bucket,
                   (ARRAY_AGG(open  ORDER BY time ASC))[1]  AS open,
                   MAX(high) AS high, MIN(low) AS low,
                   (ARRAY_AGG(close ORDER BY time DESC))[1] AS close,
                   SUM(volume) AS volume
            FROM %I GROUP BY symbol, time - time %% 1800
        $f$, tbl));

        -- 1h: bucket = time - time % 3600
        parts_1h := array_append(parts_1h, format($f$
            SELECT symbol, time - time %% 3600 AS bucket,
                   (ARRAY_AGG(open  ORDER BY time ASC))[1]  AS open,
                   MAX(high) AS high, MIN(low) AS low,
                   (ARRAY_AGG(close ORDER BY time DESC))[1] AS close,
                   SUM(volume) AS volume
            FROM %I GROUP BY symbol, time - time %% 3600
        $f$, tbl));

        -- 2h: bucket = time - time % 7200
        parts_2h := array_append(parts_2h, format($f$
            SELECT symbol, time - time %% 7200 AS bucket,
                   (ARRAY_AGG(open  ORDER BY time ASC))[1]  AS open,
                   MAX(high) AS high, MIN(low) AS low,
                   (ARRAY_AGG(close ORDER BY time DESC))[1] AS close,
                   SUM(volume) AS volume
            FROM %I GROUP BY symbol, time - time %% 7200
        $f$, tbl));

        -- 1D: bucket = (time+tz)/86400*86400 - tz  (UTC+8)
        parts_1d := array_append(parts_1d, format($f$
            SELECT symbol,
                   (time + %s) / 86400 * 86400 - %s AS bucket,
                   (ARRAY_AGG(open  ORDER BY time ASC))[1]  AS open,
                   MAX(high) AS high, MIN(low) AS low,
                   (ARRAY_AGG(close ORDER BY time DESC))[1] AS close,
                   SUM(volume) AS volume
            FROM %I GROUP BY symbol, (time + %s) / 86400 * 86400 - %s
        $f$, tz, tz, tbl, tz, tz));
    END LOOP;

    sql_text := 'CREATE OR REPLACE VIEW "kline_30m_from_15m" AS '
                || array_to_string(parts_30m, ' UNION ALL ');
    EXECUTE sql_text;
    RAISE NOTICE 'kline_30m_from_15m';

    sql_text := 'CREATE OR REPLACE VIEW "kline_1h_from_15m" AS '
                || array_to_string(parts_1h, ' UNION ALL ');
    EXECUTE sql_text;
    RAISE NOTICE 'kline_1h_from_15m';


    sql_text := 'CREATE OR REPLACE VIEW "kline_2h_from_15m" AS '
                || array_to_string(parts_2h, ' UNION ALL ');
    EXECUTE sql_text;
    RAISE NOTICE 'kline_2h_from_15m';

    sql_text := 'CREATE OR REPLACE VIEW "kline_1D_from_15m" AS '
                || array_to_string(parts_1d, ' UNION ALL ');
    EXECUTE sql_text;
    RAISE NOTICE 'kline_1D_from_15m';

    IF tables_1d IS NOT NULL AND array_length(tables_1d, 1) > 0 THEN
        FOREACH tbl IN ARRAY tables_1d LOOP
            parts_1w := array_append(parts_1w, format($f$
                SELECT symbol,
                       (((time + %s) / 86400) * 86400 - %s)
                       - (EXTRACT(DOW FROM TO_TIMESTAMP(time + %s) AT TIME ZONE 'Asia/Shanghai')
                          ::int - 1 + 7) %% 7 * 86400 AS bucket,
                       (ARRAY_AGG(open  ORDER BY time ASC))[1]  AS open,
                       MAX(high) AS high, MIN(low) AS low,
                       (ARRAY_AGG(close ORDER BY time DESC))[1] AS close,
                       SUM(volume) AS volume
                FROM %I
                GROUP BY symbol,
                         (((time + %s) / 86400) * 86400 - %s)
                         - (EXTRACT(DOW FROM TO_TIMESTAMP(time + %s) AT TIME ZONE 'Asia/Shanghai')
                            ::int - 1 + 7) %% 7 * 86400
            $f$, tz, tz, tz, tbl, tz, tz, tz));
        END LOOP;

        sql_text := 'CREATE OR REPLACE VIEW "kline_1W_from_1D" AS '
                    || array_to_string(parts_1w, ' UNION ALL ');
        EXECUTE sql_text;
        RAISE NOTICE 'kline_1W_from_1D';
    ELSE
        RAISE NOTICE 'no fine kline_1D_* skip 1W VIEW';
    END IF;

    RAISE NOTICE 'OK';
END $$;