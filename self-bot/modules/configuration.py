import json
import os

class Configuration:
    def __init__(self, file_path):
        self.file_path = file_path
        self.data = self.load_config()
    
    def load_config(self):
        if not os.path.exists(self.file_path):
            return {}
        with open(self.file_path, 'r', encoding='utf-8') as file:
            return json.load(file)
    
    def save_config(self):
        with open(self.file_path, 'w', encoding='utf-8') as file:
            json.dump(self.data, file, indent=4)
    
    def set_default(self, default):
        """Set default values if they are missing."""
        for key, value in default.items():
            if key not in self.data:
                self.data[key] = value
        self.save_config()
    
    def get(self, key, default=None):
        return self.data.get(key, default)
    
    def __getitem__(self, key):
        return self.data[key]
    
    def __setitem__(self, key, value):
        self.data[key] = value
        self.save_config()
    
    def file_exists(self, file_path):
        return os.path.exists(file_path)
    
    def flush(self):
        self.save_config()
