# AI Agents Orchestrator - Setup Script for Windows
# This script helps setup the development environment on Windows

param(
    [Parameter(Position=0)]
    [ValidateSet("requirements", "venv", "install", "env", "db", "test", "start", "all")]
    [string]$Command = "all"
)

# Color output functions
function Write-Header {
    Write-Information "=================================================="
    Write-Information "    AI Agents Orchestrator - Setup Script"
    Write-Information "=================================================="
}

function Write-Step {
    param([string]$Message)
    Write-Information "[STEP] $Message" -InformationAction Continue
}

function Write-Success {
    param([string]$Message)
    Write-Information "[SUCCESS] $Message" -InformationAction Continue
}

function Write-Error {
    param([string]$Message)
    Write-Information "[ERROR] $Message" -InformationAction Continue
}

function Check-Requirements {
    Write-Step "Checking system requirements..."
    
    # Check Python
    try {
        $pythonVersion = python --version 2>&1
        if ($pythonVersion -match "Python (\d+\.\d+)") {
            $version = [version]$matches[1]
            if ($version -lt [version]"3.9") {
                Write-Error "Python 3.9 or higher is required. Current version: $($matches[1])"
                exit 1
            }
        }
        Write-Success "Python version check passed"
    }
    catch {
        Write-Error "Python is not installed or not in PATH. Please install Python 3.9 or higher."
        exit 1
    }
    
    # Check pip
    try {
        pip --version | Out-Null
        Write-Success "pip is available"
    }
    catch {
        Write-Error "pip is not installed or not in PATH."
        exit 1
    }
    
    Write-Success "All requirements met!"
}

function Setup-VirtualEnvironment {
    Write-Step "Setting up virtual environment..."
    
    if (!(Test-Path "venv")) {
        python -m venv venv
        Write-Success "Virtual environment created"
    }
    else {
        Write-Success "Virtual environment already exists"
    }
    
    # Activate virtual environment
    & ".\venv\Scripts\Activate.ps1"
    Write-Success "Virtual environment activated"
}

function Install-Dependencies {
    Write-Step "Installing Python dependencies..."
    
    if (Test-Path "requirements.txt") {
        pip install -r requirements.txt
        Write-Success "Dependencies installed"
    }
    else {
        Write-Error "requirements.txt not found"
        exit 1
    }
}

function Setup-Environment {
    Write-Step "Setting up environment configuration..."
    
    if (!(Test-Path ".env")) {
        if (Test-Path ".env.development") {
            Copy-Item ".env.development" ".env"
            Write-Success "Environment file created from template"
        }
        else {
            Write-Error ".env.development template not found"
            exit 1
        }
    }
    else {
        Write-Success "Environment file already exists"
    }
}

function Setup-Database {
    Write-Step "Setting up MongoDB..."
    
    # Check if MongoDB is running
    try {
        $mongoProcess = Get-Process -Name "mongod" -ErrorAction SilentlyContinue
        if (!$mongoProcess) {
            Write-Error "MongoDB is not running. Please start MongoDB:"
            Write-Information "  - Using Windows Service: net start MongoDB"
            Write-Information "  - Using Docker: docker run -d -p 27017:27017 mongo:7-jammy"
            Write-Information "  - Manual start: mongod --dbpath=C:\data\db"
            exit 1
        }
    }
    catch {
        Write-Error "Could not check MongoDB status. Please ensure MongoDB is installed and running."
        exit 1
    }
    
    # Initialize database with sample data
    if (Test-Path "mongo-init\init-db.js") {
        try {
            & mongosh agno "mongo-init\init-db.js"
            Write-Success "Database initialized with sample data"
        }
        catch {
            Write-Success "MongoDB is running (could not load sample data - mongosh might not be available)"
        }
    }
    else {
        Write-Success "MongoDB is running (no sample data loaded)"
    }
}

function Run-Tests {
    Write-Step "Running tests..."
    
    try {
        pytest tests\ -v
        Write-Success "Tests completed"
    }
    catch {
        Write-Error "pytest not found. Installing..."
        pip install pytest
        pytest tests\ -v
        Write-Success "Tests completed"
    }
}

function Start-Application {
    Write-Step "Starting the application..."

    Write-Information "Starting AI Agents Orchestrator..." -InformationAction Continue
    Write-Information "Access points:" -InformationAction Continue
    Write-Information "  - API Documentation: http://localhost:7777/docs" -InformationAction Continue
    Write-Information "  - Interactive Playground: http://localhost:7777/playground" -InformationAction Continue
    Write-Information "  - Health Check: http://localhost:7777/health" -InformationAction Continue
    Write-Output ""
    Write-Information "Press Ctrl+C to stop the application" -InformationAction Continue

    python app.py
}

# Main execution
Write-Header

switch ($Command) {
    "requirements" { Check-Requirements }
    "venv" { Setup-VirtualEnvironment }
    "install" { Install-Dependencies }
    "env" { Setup-Environment }
    "db" { Setup-Database }
    "test" { Run-Tests }
    "start" { Start-Application }
    "all" {
        Check-Requirements
        Setup-VirtualEnvironment
        Install-Dependencies
        Setup-Environment
        Setup-Database
        Run-Tests
        Write-Output ""
        Write-Success "Setup completed successfully!"
        Write-Output ""
        Write-Information "To start the application, run:" -InformationAction Continue
        Write-Information "  .\venv\Scripts\Activate.ps1" -InformationAction Continue
        Write-Information "  python app.py" -InformationAction Continue
        Write-Output ""
        Write-Information "Or run: .\setup.ps1 start" -InformationAction Continue
    }
    default {
        Write-Output "Usage: .\setup.ps1 [requirements|venv|install|env|db|test|start|all]"
        Write-Output ""
        Write-Output "Commands:"
        Write-Output "  requirements  - Check system requirements"
        Write-Output "  venv         - Setup virtual environment"
        Write-Output "  install      - Install dependencies"
        Write-Output "  env          - Setup environment configuration"
        Write-Output "  db           - Setup database"
        Write-Output "  test         - Run tests"
        Write-Output "  start        - Start the application"
        Write-Output "  all          - Run all setup steps (default)"
    }
}
