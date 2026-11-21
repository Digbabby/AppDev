## 🛠️ Development Setup & Installation

This project utilizes a Python virtual environment (`venv`) to isolate dependencies. Since the `.venv` folder is excluded from version control, each developer must initialize their own environment locally after cloning the repository.

### 1. Initialize the Virtual Environment
Open your terminal in the project root directory and run the appropriate command for your operating system:

* **Windows:**
  ```bash
  python -m venv .venv
  ```
* **Mac / Linux:**
  ```bash
  python3 -m venv .venv
  ```

### 2. Activate the Environment
Before running any code or installing packages, activate the environment:

* **Windows (PowerShell):**
  ```powershell
  .venv\Scripts\Activate.ps1
  ```
  *Note: If you receive a script execution error, run `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process` in your terminal and try again.*
* **Windows (Command Prompt):**
  ```cmd
  .venv\Scripts\activate.bat
  ```
* **Mac / Linux:**
  ```bash
  source .venv/bin/activate
  ```

Your terminal prompt will display `(.venv)` at the beginning of the line once activation is successful.

### 3. Install Dependencies
With the environment activated, install all required packages using the package manager:

```bash
pip install -r requirements.txt
```

### 4. Deactivation
When you are done working on the project, you can exit the virtual environment by running:
```bash
deactivate
```
