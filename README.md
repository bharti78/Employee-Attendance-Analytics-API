# Employee Attendance & Analytics API

A FastAPI-based attendance system with MongoDB backend supporting punch-in/punch-out, manual corrections with audit trails, and analytics endpoints.

## Running the application

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables (MONGO_URI, MONGO_DB)
# Either in .env file or as real environment variables

# Start the server
uvicorn app.main:app --port 8000
```

## Environment Variables

- `MONGO_URI`: MongoDB connection string (default: mongodb://localhost:27017)
- `MONGO_DB`: Database name (default: attendance_db)

## Features Implemented

- **Employee Management**: Create and list employees with unique emp_code
- **Attendance Tracking**: Punch-in/punch-out with IST timezone handling and overnight shift support
- **Manual Corrections**: Regularize attendance records with full audit trail
- **Analytics**: Monthly summaries, department summaries, late leaderboard, and daily trends
- **Concurrency**: Race-safe operations using database unique indexes
- **Performance**: Optimized queries with proper indexes for 100k+ records
- **Explain Endpoint**: MongoDB query plan analysis for all analytics endpoints

## Business Rules Implemented

- R1: IST timezone handling with overnight shift support
- R2: Late calculation with 10-minute grace period
- R3: Overtime calculation with 30-minute minimum
- R4: Work hours with half-up rounding
- R5: Half-day detection (< 4.50 hours)
- R6: Presence counting (PRESENT, WFH, ON_DUTY)
- R7: Working days (Mon-Fri) from joined_on
- R8: Proper rounding for reported numbers
- R9: Headcount includes employees with no logs
- R10: Pagination with correct total counts

## Testing

Load sample data:
```bash
python candidate_kit/sample_seed.py
```

Health check:
```bash
curl http://localhost:8000/health
```
