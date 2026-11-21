Запуск: docker compose up --build -d

Проверка: curl http://localhost:8000/health

Добавление комнаты: curl -X POST http://localhost:8000/rooms -H "Content-Type: application/json" -d '{"capacity":8,"equipment":["screen","whiteboard"]}'

Поиск свободного окна: curl -X POST http://localhost:8000/search -H "Content-Type: application/json" -d 
'{"from_time":"2026-02-01T09:00:00+02:00","to_time":"2026-02-01T18:00:00+02:00","duration_minutes":60,"participants":["user-1","user-2"],"min_capacity":4,"equipment":["screen"]}'

Создание встречи: curl -X POST http://localhost:8000/meetings -H "Content-Type: application/json" -d '{"room_id":"uuid_из_ответа_ROOMS","start":"2026-02-01T10:00:00+02:00","duration_minutes":60,"participants":["user-1","user-2"],"occurrences":1}'

Тесты: docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from tests
