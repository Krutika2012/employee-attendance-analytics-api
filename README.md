# Employee Attendance & Analytics API

A RESTful API built with **FastAPI, Python, and MongoDB** for managing employee records, tracking attendance, and generating attendance analytics.

The API supports employee management, punch-in and punch-out operations, attendance corrections, reporting, and query execution statistics.

## 1. Features

- Employee creation and retrieval.
- Employee attendance punch-in and punch-out.
- Attendance record listing with pagination and filtering.
- Attendance record updates with validation.
- Monthly attendance analytics.
- Department-wise attendance summaries.
- Late-arrival leaderboard.
- Department attendance trends.
- MongoDB query execution statistics through the admin explain endpoint.
- Input validation and meaningful HTTP error responses.
- Automatic, idempotent MongoDB index creation at application startup.

## 2. Technology Stack

- **Language:** Python
- **Framework:** FastAPI
- **Database:** MongoDB
- **Database driver:** PyMongo
- **API documentation:** OpenAPI / Swagger UI
- **Server:** Uvicorn

## 3. Prerequisites

Ensure the following are installed before running the application:

- Python 3.9 or a compatible Python version supported by the project's dependencies.
- MongoDB Atlas or a running MongoDB instance.
- Git (optional, if cloning the repository).
- A terminal or command prompt.

## 4. Project Structure

The project is organized as follows:

```text
attendance_api/
├── app/
│   ├── __init__.py
│   └── main.py
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

The structure above shows the main application files. Additional files or modules may be present in the project.

## 5. Installation and Setup

### Step 1: Open the project directory

Open a terminal in the root directory containing `requirements.txt`.

```bash
cd attendance_api
```

If you have already opened the project directory in VS Code, you can use its integrated terminal.

### Step 2: Create a virtual environment

```bash
python -m venv venv
```

Activate the virtual environment.

**Windows — Command Prompt:**

```bat
venv\Scripts\activate
```

**Windows — PowerShell:**

```powershell
.\venv\Scripts\Activate.ps1
```

**Linux / macOS:**

```bash
source venv/bin/activate
```

### Step 3: Install dependencies

```bash
python -m pip install -r requirements.txt
```

This installs the Python packages required by the application.

## 6. MongoDB Configuration

The application requires a valid MongoDB connection.

You can use either MongoDB Atlas or a local MongoDB instance.

### Step 1: Create the environment file

Copy `.env.example` to a new file named `.env` in the project root.

**Windows — Command Prompt:**

```bat
copy .env.example .env
```

**PowerShell:**

```powershell
Copy-Item .env.example .env
```

**Linux / macOS:**

```bash
cp .env.example .env
```

### Step 2: Configure environment variables

Update `.env` with your own MongoDB connection details:

```dotenv
MONGO_URI=your_mongodb_connection_string
MONGO_DB=attendance_db
```

- `MONGO_URI`: MongoDB connection string for your database.
- `MONGO_DB`: Name of the database used by the application.

For MongoDB Atlas, obtain the connection string from your cluster's connection settings and replace the placeholders with your own credentials and cluster details.

For a local MongoDB instance, a connection string may look like:

```dotenv
MONGO_URI=mongodb://localhost:27017/
MONGO_DB=attendance_db
```

Make sure your MongoDB instance is running and the application can connect to it.

**Security note:** Never commit your actual `.env` file, database passwords, or credentials to version control. Keep `.env.example` limited to placeholder values.

Environment variables already set by the process take precedence over values loaded from `.env`.

## 7. Run the Application

From the project root, with the virtual environment activated, execute:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The API will be available at:

`http://127.0.0.1:8000`

For development with automatic reload enabled, use:

```bash
python -m uvicorn app.main:app --reload --port 8000
```

Keep the terminal open while using the API. Stop the server with `Ctrl+C`.

## 8. API Documentation and Health Check

Once the server is running, use the following URLs.

| Resource | URL |
|---|---|
| API base URL | http://127.0.0.1:8000 |
| Health check | http://127.0.0.1:8000/health |
| Swagger UI | http://127.0.0.1:8000/docs |
| ReDoc documentation | http://127.0.0.1:8000/redoc |
| OpenAPI schema | http://127.0.0.1:8000/openapi.json |

### Health Check

Send a GET request to:

```http
GET /health
```

A successful response should return HTTP `200 OK` and a status indicating that the application is healthy.

The health check confirms application availability; it should not be interpreted as proof of database connectivity unless the implementation explicitly checks MongoDB.

## 9. API Endpoint Summary

The following endpoint groups describe the API's intended functionality. Refer to `/docs` for the exact paths, HTTP methods, query parameters, request schemas, and response formats implemented in the application.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Check application health |
| `/employees` | Create and retrieve employee records |
| `/attendance/punch-in` | Record an employee's punch-in |
| `/attendance/punch-out` | Record an employee's punch-out |
| `/attendance` | Retrieve attendance records |
| `/attendance/{emp_code}/{record_date}` | Update an attendance record |
| `/analytics/monthly` | Retrieve monthly attendance analytics |
| `/analytics/department-summary` | Retrieve department-wise summaries |
| `/analytics/late-leaderboard` | Retrieve late-arrival rankings |
| `/analytics/department-trend` | Retrieve department attendance trends |
| `/admin/explain/attendance_list` | Inspect attendance query execution statistics |

**Note:** Confirm the exact route paths and supported HTTP methods in Swagger UI before making requests. The endpoint names above summarize the implemented API functionality.

## 10. Testing the API

You can test the endpoints through Swagger UI at:

`http://127.0.0.1:8000/docs`

Recommended testing sequence:

1. Start the server and verify `GET /health`.
2. Create an employee using the employee creation endpoint.
3. Retrieve employees and verify the employee record.
4. Record a punch-in for a valid employee.
5. Record a punch-out after the punch-in.
6. Retrieve attendance records and verify the stored data.
7. Test attendance updates with valid and invalid inputs.
8. Test monthly and department-level analytics.
9. Test the late-arrival leaderboard and department trend.
10. Test the admin explain endpoint with valid query parameters.
11. Verify validation errors for invalid employee codes, timestamps, dates, and other unsupported inputs.

Use valid request bodies and query parameters as defined in Swagger UI. For attendance operations, ensure the employee exists and the timestamps satisfy the API's validation rules.

## 11. Data and Validation Notes

- **Employee identity:** `emp_code` is the public employee identifier. MongoDB internal `_id` values are not exposed as employee identifiers in API responses.
- **Timestamp format:** API timestamps are integer epoch milliseconds.
- **Database timestamps:** Instants are stored in MongoDB as BSON datetime values.
- **Attendance validation:** Punch-out must occur after punch-in and must satisfy the application's maximum allowed duration.
- **Derived fields:** Fields calculated by the application are read-only where specified by the API contract.
- **Indexing:** Required MongoDB indexes are created at application startup. Index creation is designed to be idempotent.
- **Date handling:** Attendance dates and reporting periods must follow the API's documented date and timezone conventions.

## 12. MongoDB Compatibility

The department trend aggregation uses MongoDB aggregation stages including `$documents`, `$lookup`, and `$setWindowFields`.

The `$documents` stage requires MongoDB 5.1 or later. Use a MongoDB version that supports all aggregation stages used by the application and is compatible with the configured deployment.

If an aggregation endpoint fails, verify the MongoDB server version and review the application logs.

## 13. Troubleshooting

### MongoDB connection failure

- Verify `MONGO_URI` and `MONGO_DB` in `.env`.
- Confirm the MongoDB server is running or the Atlas cluster is accessible.
- For Atlas, check network access rules and database-user permissions.
- Ensure special characters in database passwords are URL-encoded when required in the connection string.

### Missing Python packages

Activate the virtual environment and run:

```bash
python -m pip install -r requirements.txt
```

### Port already in use

Start the application on another available port, for example:

```bash
python -m uvicorn app.main:app --port 8001
```

Use the corresponding port when opening Swagger UI or the health-check URL.

### Application import error

Run Uvicorn from the project root directory, where the `app` package is available.

### Analytics endpoint error

Check the MongoDB server version, database connectivity, request parameters, and application logs.

## 14. Security and Submission Checklist

Before submitting the project:

- Keep `.env` out of the submission ZIP and version control.
- Include `.env.example` with placeholders only.
- Include `requirements.txt`, `README.md`, and the application source code.
- Exclude the virtual environment, `__pycache__`, and `.pyc` files.
- Verify that the application starts using the documented command.
- Verify `/health` and `/docs`.
- Ensure the evaluator can configure their own MongoDB connection.
- Do not include database passwords, access tokens, or other secrets.

## 15. License

This project was developed as an Employee Attendance & Analytics API assignment. No separate open-source license is specified.
