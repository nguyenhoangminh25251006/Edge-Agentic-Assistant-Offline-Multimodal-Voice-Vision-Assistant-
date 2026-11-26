"""Import before tensorflow in the GPU venv (.venv-gpu, TF 2.10 = last native-Windows GPU build):
puts the pip-installed CUDA 11 / cuDNN 8 DLLs on the DLL search path."""
import os, site, glob
for sp in site.getsitepackages():
    for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
        os.add_dll_directory(d)
        os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
