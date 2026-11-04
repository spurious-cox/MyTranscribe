/* MyTranscribe launcher: a real Mach-O so macOS attributes Screen Recording
 * to "MyTranscribe" (the responsible process) instead of the Xcode Python. */
#include <spawn.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>
extern char **environ;
int main(int argc, char **argv) {
    setenv("TK_SILENCE_DEPRECATION", "1", 1);
    char *args[argc + 3];
    int n = 0;
    args[n++] = "python3";
    args[n++] = SCRIPT;
    for (int i = 1; i < argc; i++) args[n++] = argv[i];
    args[n] = NULL;
    pid_t pid;
    if (posix_spawn(&pid, PYTHON, NULL, NULL, args, environ) != 0) return 127;
    int st = 0;
    waitpid(pid, &st, 0);
    return WIFEXITED(st) ? WEXITSTATUS(st) : 1;
}
