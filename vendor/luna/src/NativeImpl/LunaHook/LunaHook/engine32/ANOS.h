
// あの、素晴らしい　をもう一度 for Windows

class ANOS : public ENGINE
{
public:
    ANOS()
    {

        check_by = CHECK_BY::FILE_ALL;
        check_by_target = check_by_list{L"anos.dat", L"MOVIE/*.AVI", L"MP3/*.MP3"};
        is_engine_certain = false;
    };
    bool attach_function();
};