#!/bin/bash

# AI Agents Orchestrator - Setup Script
# This script helps setup the development environment

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Functions
print_header() {
    echo -e "${BLUE}"
    echo "=================================================="
    echo "    AI Agents Orchestrator - Setup Script"
    echo "=================================================="
    echo -e "${NC}"
}

print_step() {
    echo -e "${YELLOW}[STEP]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_requirements() {
    print_step "Checking system requirements..."
    
    # Check Python
    if ! command -v python3 &> /dev/null; then
        print_error "Python 3 is not installed. Please install Python 3.9 or higher."
        exit 1
    fi
    
    PYTHON_VERSION=$(python3 --version | cut -d' ' -f2 | cut -d'.' -f1-2)
    if [[ $(echo "$PYTHON_VERSION >= 3.9" | bc -l) -eq 0 ]]; then
        print_error "Python 3.9 or higher is required. Current version: $PYTHON_VERSION"
        exit 1
    fi
    
    # Check pip
    if ! command -v pip &> /dev/null && ! command -v pip3 &> /dev/null; then
        print_error "pip is not installed. Please install pip."
        exit 1
    fi
    
    # Check MongoDB
    if ! command -v mongod &> /dev/null; then
        print_error "MongoDB is not installed. Please install MongoDB or use Docker."
        echo "You can run: docker run -d -p 27017:27017 mongo:7-jammy"
        exit 1
    fi
    
    print_success "All requirements met!"
}

setup_virtual_environment() {
    print_step "Setting up virtual environment..."
    
    if [ ! -d "venv" ]; then
        python3 -m venv venv
        print_success "Virtual environment created"
    else
        print_success "Virtual environment already exists"
    fi
    
    source venv/bin/activate
    print_success "Virtual environment activated"
}

install_dependencies() {
    print_step "Installing Python dependencies..."
    
    if [ -f "requirements.txt" ]; then
        pip install -r requirements.txt
        print_success "Dependencies installed"
    else
        print_error "requirements.txt not found"
        exit 1
    fi
}

setup_environment() {
    print_step "Setting up environment configuration..."
    
    if [ ! -f ".env" ]; then
        if [ -f ".env.development" ]; then
            cp .env.development .env
            print_success "Environment file created from template"
        else
            print_error ".env.development template not found"
            exit 1
        fi
    else
        print_success "Environment file already exists"
    fi
}

setup_database() {
    print_step "Setting up MongoDB..."
    
    # Check if MongoDB is running
    if ! pgrep -x "mongod" > /dev/null; then
        print_error "MongoDB is not running. Please start MongoDB:"
        echo "  - Using systemd: sudo systemctl start mongod"
        echo "  - Using Docker: docker run -d -p 27017:27017 mongo:7-jammy"
        exit 1
    fi
    
    # Initialize database with sample data
    if [ -f "mongo-init/init-db.js" ]; then
        mongosh agno mongo-init/init-db.js
        print_success "Database initialized with sample data"
    else
        print_success "MongoDB is running (no sample data loaded)"
    fi
}

run_tests() {
    print_step "Running tests..."
    
    if command -v pytest &> /dev/null; then
        pytest tests/ -v
        print_success "Tests completed"
    else
        print_error "pytest not installed. Installing..."
        pip install pytest
        pytest tests/ -v
        print_success "Tests completed"
    fi
}

start_application() {
    print_step "Starting the application..."
    
    echo "Starting AI Agents Orchestrator..."
    echo "Access points:"
    echo "  - API Documentation: http://localhost:7777/docs"
    echo "  - Interactive Playground: http://localhost:7777/playground"
    echo "  - Health Check: http://localhost:7777/health"
    echo ""
    echo "Press Ctrl+C to stop the application"
    
    python app.py
}

# Main execution
main() {
    print_header
    
    case "${1:-all}" in
        "requirements")
            check_requirements
            ;;
        "venv")
            setup_virtual_environment
            ;;
        "install")
            install_dependencies
            ;;
        "env")
            setup_environment
            ;;
        "db")
            setup_database
            ;;
        "test")
            run_tests
            ;;
        "start")
            start_application
            ;;
        "all")
            check_requirements
            setup_virtual_environment
            install_dependencies
            setup_environment
            setup_database
            run_tests
            echo ""
            print_success "Setup completed successfully!"
            echo ""
            echo "To start the application, run:"
            echo "  source venv/bin/activate"
            echo "  python app.py"
            echo ""
            echo "Or run: ./setup.sh start"
            ;;
        *)
            echo "Usage: $0 [requirements|venv|install|env|db|test|start|all]"
            echo ""
            echo "Commands:"
            echo "  requirements  - Check system requirements"
            echo "  venv         - Setup virtual environment"
            echo "  install      - Install dependencies"
            echo "  env          - Setup environment configuration"
            echo "  db           - Setup database"
            echo "  test         - Run tests"
            echo "  start        - Start the application"
            echo "  all          - Run all setup steps (default)"
            ;;
    esac
}

# Run main function with all arguments
main "$@"
